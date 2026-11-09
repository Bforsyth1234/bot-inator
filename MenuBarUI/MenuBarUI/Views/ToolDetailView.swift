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
        case sourcePath = "source_path"
        case errorText = "error_text"
    }
}

@MainActor
final class ToolDetailViewModel: ObservableObject {
    @Published private(set) var detail: ToolDetail?
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
                .font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(toolName).font(.system(.headline, design: .monospaced))
                if let detail = viewModel.detail {
                    Text(subtitle(detail)).font(.caption).foregroundStyle(.secondary)
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
            Text(detail.summary.isEmpty ? "No description." : detail.summary)
        }
        section("When it runs") {
            whenItRuns(detail)
            Label(detail.runsFreely
                  ? "Runs without asking (read-only)"
                  : "Needs your approval each time",
                  systemImage: detail.runsFreely ? "bolt.fill" : "hand.raised.fill")
                .foregroundStyle(detail.runsFreely ? Color.secondary : Color.accentColor)
                .font(.callout)
        }
        if !detail.inputs.isEmpty {
            section("Inputs") {
                ForEach(detail.inputs, id: \.self) { input in
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(input.name): \(input.type)\(input.optional ? " (optional)" : "")")
                            .font(.system(.callout, design: .monospaced))
                        if !input.description.isEmpty {
                            Text(input.description).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        if let sandbox = detail.sandbox {
            section("Sandbox") {
                Label(sandbox.networkAllowed ? "Network allowed" : "No network",
                      systemImage: sandbox.networkAllowed ? "network" : "network.slash")
                Text(sandbox.note).font(.caption).foregroundStyle(.secondary)
            }
        }
        if detail.state == "quarantined" {
            section("Load error") {
                Text(detail.errorText?.isEmpty == false ? detail.errorText! : "No error text saved.")
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .foregroundStyle(.orange)
            }
        }
        if let path = detail.sourcePath {
            section("Source") {
                Text(path)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .foregroundStyle(.secondary)
            }
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
            Text("• \(hint)").font(.callout).foregroundStyle(.secondary)
        }
        if let prov = when.provenance {
            VStack(alignment: .leading, spacing: 2) {
                if let logic = prov.idea?.expectedLogic ?? prov.requested?.expectedLogic, !logic.isEmpty {
                    Text("Expected logic: \(logic)").font(.caption)
                }
                if let evidence = prov.idea?.evidence, !evidence.isEmpty {
                    Text("Evidence: \(evidence)").font(.caption).lineLimit(4)
                }
                if let installed = prov.installedAt {
                    Text("Installed \(installed)\(prov.userEdited == true ? " · edited by you before install" : "")")
                        .font(.caption)
                }
            }
            .foregroundStyle(.secondary)
        }
    }

    private func section<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.subheadline.weight(.semibold))
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
