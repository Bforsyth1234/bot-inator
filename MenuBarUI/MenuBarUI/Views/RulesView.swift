import SwiftUI

/// Standing rules (#78): "always do this". After the user approves the same
/// action for the same kind of trigger 5 times without changes, the daemon
/// offers a rule; only the user's "Turn on" here creates one. Every rule is
/// listed with on/off, delete and the evidence that led to it; every
/// automatic run is listed with Undo. A rule undone twice switches itself off.
///
/// Routes: `GET /api/rules`, `POST /api/rules/offers/{id}/accept|dismiss`,
/// `PATCH/DELETE /api/rules/{id}`, `POST /api/rules/runs/{entry_id}/undo`.

struct RuleEvidence: Codable, Hashable {
    let cleanApprovals: Int?
    let text: String?

    enum CodingKeys: String, CodingKey {
        case cleanApprovals = "clean_approvals"
        case text
    }
}

struct StandingRule: Codable, Hashable, Identifiable {
    let id: String
    let tool: String
    let words: String
    let enabled: Bool
    let disabledReason: String?
    let evidence: RuleEvidence?
    let runs: Int?
    let undos: Int?

    enum CodingKeys: String, CodingKey {
        case id, tool, words, enabled, evidence, runs, undos
        case disabledReason = "disabled_reason"
    }
}

struct RuleOffer: Codable, Hashable, Identifiable {
    let id: String
    let words: String
    let evidence: RuleEvidence?
}

struct RuleRun: Codable, Hashable, Identifiable {
    let id: String
    let createdAt: Double
    let label: String
    let canUndo: Bool
    let undoStatus: String?
    let undoMessage: String?

    enum CodingKeys: String, CodingKey {
        case id, label
        case createdAt = "created_at"
        case canUndo = "can_undo"
        case undoStatus = "undo_status"
        case undoMessage = "undo_message"
    }
}

struct RulesListing: Codable {
    let rules: [StandingRule]
    let offers: [RuleOffer]
    let runs: [RuleRun]
}

@MainActor
final class RulesViewModel: ObservableObject {
    @Published private(set) var rules: [StandingRule] = []
    @Published private(set) var offers: [RuleOffer] = []
    @Published private(set) var runs: [RuleRun] = []
    @Published var errorMessage: String?
    @Published var infoMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private func send(_ path: String, method: String, body: Data? = nil) async throws -> Data {
        var request = try DaemonAuth.request(baseURL.appendingPathComponent(path), method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = (json?["detail"] as? String) ?? (json?["message"] as? String)
            throw NSError(domain: "Rules", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        do {
            let data = try await send("api/rules", method: "GET")
            let listing = try JSONDecoder().decode(RulesListing.self, from: data)
            rules = listing.rules
            offers = listing.offers
            runs = listing.runs
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load rules: \(error.localizedDescription)"
        }
    }

    func accept(_ offer: RuleOffer) async {
        await act("api/rules/offers/\(offer.id)/accept", method: "POST", what: "turn on the rule")
    }

    func dismiss(_ offer: RuleOffer) async {
        await act("api/rules/offers/\(offer.id)/dismiss", method: "POST", what: "dismiss the offer")
    }

    func setEnabled(_ rule: StandingRule, _ enabled: Bool) async {
        let body = try? JSONEncoder().encode(["enabled": enabled])
        await act("api/rules/\(rule.id)", method: "PATCH", body: body, what: "update the rule")
    }

    func remove(_ rule: StandingRule) async {
        await act("api/rules/\(rule.id)", method: "DELETE", what: "delete the rule")
    }

    func undo(_ run: RuleRun) async {
        do {
            let data = try await send("api/rules/runs/\(run.id)/undo", method: "POST")
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            infoMessage = (json?["message"] as? String) ?? "Undone."
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        await refresh()
    }

    private func act(_ path: String, method: String, body: Data? = nil, what: String) async {
        do {
            _ = try await send(path, method: method, body: body)
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't \(what): \(error.localizedDescription)"
        }
        await refresh()
    }
}

/// Self-contained "Standing rules" section: embed with `RulesView()`.
struct RulesView: View {
    @StateObject private var viewModel = RulesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Text("A rule does one thing without asking, only after you approved it 5 times without changes. It never sends, drafts, deletes, runs Shortcuts or uses the network. Every run can be undone; a rule you undo twice switches itself off.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if let info = viewModel.infoMessage {
                Text(info).appFont(.secondary).foregroundStyle(.secondary)
            }
            if !viewModel.offers.isEmpty {
                Text("Always do this?").appFont(.label)
                ForEach(viewModel.offers) { offer in
                    offerBox(offer)
                }
            }
            rulesList
            if !viewModel.runs.isEmpty {
                Text("Recent automatic runs").appFont(.label)
                ForEach(viewModel.runs) { run in
                    runRow(run)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "bolt.badge.automatic")
                .foregroundStyle(.secondary)
            Text("Standing rules").appFont(.headline)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload rules")
        }
    }

    private func offerBox(_ offer: RuleOffer) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(offer.words).appFont(.body).fixedSize(horizontal: false, vertical: true)
            if let text = offer.evidence?.text {
                Text(text).appFont(.fine).foregroundStyle(.secondary)
            }
            HStack {
                Button("Turn on") { Task { await viewModel.accept(offer) } }
                    .keyboardShortcut(.defaultAction)
                Button("No thanks") { Task { await viewModel.dismiss(offer) } }
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
    }

    @ViewBuilder
    private var rulesList: some View {
        if viewModel.rules.isEmpty {
            Text("No rules yet.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else {
            ForEach(viewModel.rules) { rule in
                ruleRow(rule)
            }
        }
    }

    private func ruleRow(_ rule: StandingRule) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Toggle("", isOn: Binding(
                get: { rule.enabled },
                set: { value in Task { await viewModel.setEnabled(rule, value) } }))
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(.small)
            VStack(alignment: .leading, spacing: 2) {
                Text(rule.words).appFont(.body).lineLimit(3)
                if let text = rule.evidence?.text {
                    Text(text).appFont(.fine).foregroundStyle(.secondary)
                }
                Text("Ran \(rule.runs ?? 0) times · undone \(rule.undos ?? 0) since switched on")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                if let reason = rule.disabledReason, !reason.isEmpty, !rule.enabled {
                    Text(reason).appFont(.fine).foregroundStyle(.orange)
                }
            }
            Spacer()
            Button("Delete", role: .destructive) {
                Task { await viewModel.remove(rule) }
            }
            .controlSize(.small)
        }
        .padding(.vertical, 2)
    }

    private func runRow(_ run: RuleRun) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(Date(timeIntervalSince1970: run.createdAt), style: .relative)
                .appFont(.fine)
                .foregroundStyle(.secondary)
            Text(run.label).appFont(.secondary).lineLimit(1)
            Spacer()
            if run.canUndo {
                Button("Undo") { Task { await viewModel.undo(run) } }
                    .controlSize(.small)
            } else if let message = run.undoMessage, !message.isEmpty {
                Text(message).appFont(.fine).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }
}
