import AppKit
import SwiftUI

/// ``GET /api/tools/<name>`` (#61): what a tool does, when the agent calls
/// it, whether it asks first, and where its source lives.
struct ToolDetail: Decodable, Equatable {
    struct Input: Decodable, Equatable, Hashable {
        let name: String
        let type: String
        let description: String
        let optional: Bool
    }

    struct Provenance: Decodable, Equatable {
        struct Requested: Decodable, Equatable {
            let description: String?
            let expectedLogic: String?
            enum CodingKeys: String, CodingKey {
                case description
                case expectedLogic = "expected_logic"
            }
        }

        struct Idea: Decodable, Equatable {
            let source: String?
            let trigger: String?
            let description: String?
            let expectedLogic: String?
            let evidence: String?
            enum CodingKeys: String, CodingKey {
                case source, trigger, description, evidence
                case expectedLogic = "expected_logic"
            }
        }

        let installedAt: String?
        let turnKind: String?
        let source: String?
        let trigger: String?
        let idea: Idea?
        let requested: Requested?
        let userEdited: Bool?

        enum CodingKeys: String, CodingKey {
            case source, trigger, idea, requested
            case installedAt = "installed_at"
            case turnKind = "turn_kind"
            case userEdited = "user_edited"
        }
    }

    struct WhenItRuns: Decodable, Equatable {
        let text: String
        let hints: [String]
        let provenance: Provenance?
    }

    struct Sandbox: Decodable, Equatable {
        let enabled: Bool
        let networkAllowed: Bool
        let note: String
        enum CodingKeys: String, CodingKey {
            case enabled, note
            case networkAllowed = "network_allowed"
        }
    }

    let name: String
    let kind: String
    let state: String
    let summary: String
    /// Plain-language summary saved at install time (#64); nil for
    /// built-ins, older installs and older daemons.
    let plainSummary: String?
    let whenItRuns: WhenItRuns
    let gate: String
    let inputs: [Input]
    let sandbox: Sandbox?
    let sourcePath: String?
    let errorText: String?

    var isGenerated: Bool { kind == "generated" }
    var runsFreely: Bool { gate == "runs without asking" }

    enum CodingKeys: String, CodingKey {
        case name, kind, state, summary, gate, inputs, sandbox
        case whenItRuns = "when_it_runs"
        case plainSummary = "plain_summary"
        case sourcePath = "source_path"
        case errorText = "error_text"
    }
}

/// ``GET /api/tools/<name>/health`` (#90): recent failures of a generated
/// tool and whether a previous version can be restored.
struct ToolHealth: Decodable, Equatable {
    struct Failure: Decodable, Equatable, Hashable {
        let at: String?
        let errorClass: String?
        let message: String?
        let repairable: Bool
        let category: String?
        let reason: String?
        enum CodingKeys: String, CodingKey {
            case at, message, repairable, category, reason
            case errorClass = "error_class"
        }
    }

    struct Change: Decodable, Equatable {
        let kind: String
        let at: String?
    }

    let status: String
    let okRuns: Int
    let failures7d: Int
    let recentFailures: [Failure]
    let lastChange: Change?
    let repairOfferedUntil: String?
    let canRestore: Bool?

    enum CodingKeys: String, CodingKey {
        case status
        case okRuns = "ok_runs"
        case failures7d = "failures_7d"
        case recentFailures = "recent_failures"
        case lastChange = "last_change"
        case repairOfferedUntil = "repair_offered_until"
        case canRestore = "can_restore"
    }
}

@MainActor
final class ToolDetailViewModel: ObservableObject {
    @Published private(set) var detail: ToolDetail?
    /// nil for built-ins, older daemons, or before the health log is ready.
    @Published private(set) var health: ToolHealth?
    @Published private(set) var isRestoring = false
    @Published var restoreMessage: String?
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func load(_ name: String) async {
        isLoading = true
        defer { isLoading = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/tools/\(name)")
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw NSError(domain: "ToolDetail", code: http.statusCode, userInfo: [
                    NSLocalizedDescriptionKey: http.statusCode == 404
                        ? "No tool named \(name)" : "HTTP \(http.statusCode)",
                ])
            }
            detail = try JSONDecoder().decode(ToolDetail.self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load \(name): \(error.localizedDescription)"
        }
        if detail?.isGenerated == true { await loadHealth(name) }
    }

    /// Best effort: a missing route or log leaves the section hidden.
    func loadHealth(_ name: String) async {
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/tools/\(name)/health")
            )
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                health = nil
                return
            }
            health = try JSONDecoder().decode(ToolHealth.self, from: data)
        } catch {
            health = nil
        }
    }

    /// Put back the tool's prior committed version (#90): a user click, no card.
    func restorePrevious(_ name: String) async {
        isRestoring = true
        defer { isRestoring = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/tools/\(name)/restore-previous"),
                method: "POST"
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
                restoreMessage = "Restore failed: \(detail as? String ?? "HTTP \(http.statusCode)")"
                return
            }
            restoreMessage = "Previous version restored and reloaded."
        } catch {
            restoreMessage = "Restore failed: \(error.localizedDescription)"
        }
        await load(name)
    }
}

struct ToolDetailView: View {
    let toolName: String
    @StateObject private var viewModel = ToolDetailViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let error = viewModel.errorMessage {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                    }
                    if let detail = viewModel.detail {
                        sections(detail)
                    } else if viewModel.isLoading {
                        ProgressView().frame(maxWidth: .infinity)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 480, minHeight: 440)
        .task { await viewModel.load(toolName) }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: viewModel.detail?.isGenerated == true ? "sparkles" : "hammer")
                .foregroundStyle(viewModel.detail?.isGenerated == true ? .purple : .secondary)
                .appFont(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(toolName).appFont(.headline, design: .monospaced)
                if let detail = viewModel.detail {
                    Text(subtitle(detail)).appFont(.secondary).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if let path = viewModel.detail?.sourcePath {
                Button("Open in editor") { Self.openInEditor(path) }
                    .help(path)
            }
            Button("Done") { dismiss() }
                .keyboardShortcut(.defaultAction)
        }
        .padding(14)
    }

    private func subtitle(_ detail: ToolDetail) -> String {
        let kind = detail.isGenerated ? "Generated" : "Built-in"
        switch detail.state {
        case "disabled": return "\(kind) · disabled"
        case "quarantined": return "\(kind) · failed to load"
        case "not_loaded": return "\(kind) · not loaded yet"
        default: return kind
        }
    }

    @ViewBuilder
    private func sections(_ detail: ToolDetail) -> some View {
        section("What it does") {
            if let plain = detail.plainSummary, !plain.isEmpty {
                // The same sentences the idea and code-review cards showed.
                Text(plain).textSelection(.enabled)
                if !detail.summary.isEmpty {
                    Text(detail.summary).appFont(.secondary).foregroundStyle(.secondary)
                }
            } else {
                Text(detail.summary.isEmpty ? "No description." : detail.summary)
            }
        }
        section("When it runs") {
            whenItRuns(detail)
            Label(detail.runsFreely
                  ? "Runs without asking (read-only)"
                  : "Needs your approval each time",
                  systemImage: detail.runsFreely ? "bolt.fill" : "hand.raised.fill")
                .foregroundStyle(detail.runsFreely ? Color.secondary : Color.accentColor)
                .appFont(.callout)
        }
        if !detail.inputs.isEmpty {
            section("Inputs") {
                ForEach(detail.inputs, id: \.self) { input in
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(input.name): \(input.type)\(input.optional ? " (optional)" : "")")
                            .appFont(.callout, design: .monospaced)
                        if !input.description.isEmpty {
                            Text(input.description).appFont(.secondary).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        if let sandbox = detail.sandbox {
            section("Sandbox") {
                Label(sandbox.networkAllowed ? "Network allowed" : "No network",
                      systemImage: sandbox.networkAllowed ? "network" : "network.slash")
                Text(sandbox.note).appFont(.secondary).foregroundStyle(.secondary)
            }
        }
        if detail.isGenerated, let health = viewModel.health {
            healthSection(health)
        }
        if detail.state == "quarantined" {
            section("Load error") {
                Text(detail.errorText?.isEmpty == false ? detail.errorText! : "No error text saved.")
                    .appFont(.secondary, design: .monospaced)
                    .textSelection(.enabled)
                    .foregroundStyle(.orange)
            }
        }
        if let path = detail.sourcePath {
            section("Source") {
                Text(path)
                    .appFont(.secondary, design: .monospaced)
                    .textSelection(.enabled)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func healthSection(_ health: ToolHealth) -> some View {
        section("Health") {
            Label(healthLine(health),
                  systemImage: health.status == "failing"
                    ? "exclamationmark.triangle.fill" : "checkmark.seal")
                .foregroundStyle(health.status == "failing" ? Color.orange : Color.secondary)
            if let change = health.lastChange {
                Text("Last \(change.kind == "restored" ? "restored" : "repaired") \(change.at ?? "")")
                    .appFont(.secondary).foregroundStyle(.secondary)
            }
            if health.repairOfferedUntil != nil {
                Text("A repair was offered recently; the next offer waits a week.")
                    .appFont(.secondary).foregroundStyle(.secondary)
            }
            ForEach(Array(health.recentFailures.prefix(5).enumerated()), id: \.offset) { _, failure in
                VStack(alignment: .leading, spacing: 1) {
                    Text("\(failure.at ?? "") · \(failure.errorClass ?? "error")")
                        .appFont(.secondary, weight: .semibold)
                    if let message = failure.message, !message.isEmpty {
                        Text(message)
                            .appFont(.secondary, design: .monospaced)
                            .textSelection(.enabled)
                            .lineLimit(3)
                    }
                    if !failure.repairable, let reason = failure.reason {
                        Text("Not repairable: \(reason)")
                            .appFont(.secondary).foregroundStyle(.secondary)
                    }
                }
            }
            if health.canRestore == true {
                HStack {
                    Button("Restore previous version") {
                        Task { await viewModel.restorePrevious(toolName) }
                    }
                    .disabled(viewModel.isRestoring)
                    .help("Put back the version installed before the last change and reload it")
                    if viewModel.isRestoring { ProgressView().controlSize(.small) }
                }
            }
            if let message = viewModel.restoreMessage {
                Text(message).appFont(.secondary).foregroundStyle(.secondary)
            }
        }
    }

    private func healthLine(_ health: ToolHealth) -> String {
        switch health.status {
        case "failing": return "Failing · \(health.failures7d) failure\(health.failures7d == 1 ? "" : "s") in the last 7 days"
        case "ok": return "Working · \(health.okRuns) successful run\(health.okRuns == 1 ? "" : "s")"
        default: return "No runs recorded yet"
        }
    }

    @ViewBuilder
    private func whenItRuns(_ detail: ToolDetail) -> some View {
        let when = detail.whenItRuns
        if detail.isGenerated && when.provenance == nil {
            Text("Unknown: this tool was installed before bot-inator saved where its ideas came from.")
                .foregroundStyle(.secondary)
        } else {
            Text(when.text)
        }
        ForEach(when.hints, id: \.self) { hint in
            Text("• \(hint)").appFont(.callout).foregroundStyle(.secondary)
        }
        if let prov = when.provenance {
            VStack(alignment: .leading, spacing: 2) {
                if let logic = prov.idea?.expectedLogic ?? prov.requested?.expectedLogic, !logic.isEmpty {
                    Text("Expected logic: \(logic)").appFont(.secondary)
                }
                if let evidence = prov.idea?.evidence, !evidence.isEmpty {
                    Text("Evidence: \(evidence)").appFont(.secondary).lineLimit(4)
                }
                if let installed = prov.installedAt {
                    Text("Installed \(installed)\(prov.userEdited == true ? " · edited by you before install" : "")")
                        .appFont(.secondary)
                }
            }
            .foregroundStyle(.secondary)
        }
    }

    private func section<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).appFont(.label, weight: .semibold)
            content()
        }
    }

    /// Open the file in the user's default text editor via `open -t`.
    /// Not `NSWorkspace.open(url)`: the default app for `.py` can be
    /// Python Launcher, which would RUN the unreviewed module.
    static func openInEditor(_ path: String) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-t", path]
        do {
            try task.run()
        } catch {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        }
    }
}
