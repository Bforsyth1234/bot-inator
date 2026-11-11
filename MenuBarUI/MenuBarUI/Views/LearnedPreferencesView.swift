import SwiftUI

/// One row of ``GET /api/preferences/learned`` (#72): a preference the
/// daemon learned from approval cards (repeated denials, shortened drafts,
/// a typed note). Deleting one stops it being recalled; a stopped offer
/// may then return.
struct LearnedPreference: Codable, Hashable, Identifiable {
    let id: Int
    let text: String
    let rule: String
    let toolName: String
    let triggerKind: String
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, text, rule
        case toolName = "tool_name"
        case triggerKind = "trigger_kind"
        case createdAt = "created_at"
    }

    var ruleLabel: String {
        switch rule {
        case "stop_offer": return "Stopped offer"
        case "short_drafts": return "Draft style"
        case "note": return "Your note"
        default: return "Learned"
        }
    }

    var ruleSymbol: String {
        switch rule {
        case "stop_offer": return "hand.raised"
        case "short_drafts": return "text.alignleft"
        case "note": return "note.text"
        default: return "lightbulb"
        }
    }
}

@MainActor
final class LearnedPreferencesViewModel: ObservableObject {
    @Published private(set) var preferences: [LearnedPreference] = []
    @Published private(set) var isLoading: Bool = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/preferences/learned"))
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            preferences = try JSONDecoder().decode([LearnedPreference].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to fetch learned preferences: \(error.localizedDescription)"
        }
    }

    func remove(_ preference: LearnedPreference) async {
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/preferences/learned/\(preference.id)"),
                method: "DELETE")
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to delete preference: \(error.localizedDescription)"
        }
    }

    private static func checkStatus(_ response: URLResponse) throws {
        try DaemonAuth.checkAuth(response)
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            throw NSError(
                domain: "LearnedPreferences", code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
    }
}

/// Self-contained "Learned preferences" section: embed with
/// `LearnedPreferencesView()`.
struct LearnedPreferencesView: View {
    @StateObject private var viewModel = LearnedPreferencesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if viewModel.preferences.isEmpty {
                Text("Nothing learned yet. When you deny the same offer three times, shorten drafts, or type a note on a card, it shows here. A learned preference never lets anything run without asking.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            } else {
                List {
                    ForEach(viewModel.preferences) { pref in
                        row(for: pref)
                    }
                }
                .listStyle(.inset)
                .frame(minHeight: 120, maxHeight: 220)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "brain")
                .foregroundStyle(.secondary)
            Text("Learned preferences").appFont(.headline)
            Text("\(viewModel.preferences.count)")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(viewModel.isLoading)
            .help("Reload learned preferences")
        }
    }

    private func row(for pref: LearnedPreference) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: pref.ruleSymbol)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(pref.text)
                    .appFont(.body)
                    .lineLimit(3)
                Text(pref.createdAt.isEmpty ? pref.ruleLabel : "\(pref.ruleLabel) · \(pref.createdAt)")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Delete", role: .destructive) {
                Task { await viewModel.remove(pref) }
            }
            .controlSize(.small)
            .help(pref.rule == "stop_offer"
                  ? "Forget this: the offer may come back"
                  : "Forget this preference")
        }
        .padding(.vertical, 2)
    }
}
