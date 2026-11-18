import SwiftUI

/// Always-allow entries (#116): the user pressed "Always allow" on a card
/// whose action can be undone, so that tool for that kind of trigger no
/// longer asks. Listed here so the user can see and revoke each one; an
/// entry undone twice switches itself off.
///
/// Routes: `GET /api/always-allow`, `DELETE /api/always-allow/{key}`.

struct AlwaysAllowEntry: Codable, Hashable, Identifiable {
    let key: String
    let tool: String
    let trigger: String
    let enabled: Bool
    let runs: Int?
    let undos: Int?
    let addedAt: Double?
    let lastRunAt: Double?
    let words: String

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, tool, trigger, enabled, runs, undos, words
        case addedAt = "added_at"
        case lastRunAt = "last_run_at"
    }
}

struct AlwaysAllowListing: Codable {
    let entries: [AlwaysAllowEntry]
}

@MainActor
final class AlwaysAllowViewModel: ObservableObject {
    @Published private(set) var entries: [AlwaysAllowEntry] = []
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// The key holds "|", so encode everything but unreserved characters.
    static func encodedKey(_ key: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return key.addingPercentEncoding(withAllowedCharacters: allowed) ?? key
    }

    private func send(_ path: String, method: String) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        let request = try DaemonAuth.request(url, method: method)
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = (json?["detail"] as? String) ?? (json?["message"] as? String)
            throw NSError(domain: "AlwaysAllow", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        do {
            let data = try await send("/api/always-allow", method: "GET")
            entries = try JSONDecoder().decode(AlwaysAllowListing.self, from: data).entries
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load Always allow: \(error.localizedDescription)"
        }
    }

    func remove(_ entry: AlwaysAllowEntry) async {
        do {
            _ = try await send("/api/always-allow/\(Self.encodedKey(entry.key))", method: "DELETE")
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't remove it: \(error.localizedDescription)"
        }
        await refresh()
    }
}

/// Self-contained "Always allow" section: embed with `AlwaysAllowView()`.
struct AlwaysAllowView: View {
    @StateObject private var viewModel = AlwaysAllowViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if viewModel.entries.isEmpty {
                Text("Nothing yet. Cards with an action you can undo offer an \u{201C}Always allow\u{201D} button.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(viewModel.entries) { entry in
                    row(entry)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.seal")
                .foregroundStyle(.secondary)
            Text("Always allow").appFont(.headline)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload")
        }
    }

    private func row(_ entry: AlwaysAllowEntry) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.words).appFont(.body).lineLimit(3)
                Text(runsText(entry))
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                if !entry.enabled {
                    Text("Off (undone twice)").appFont(.fine).foregroundStyle(.orange)
                }
            }
            Spacer()
            Button("Remove", role: .destructive) {
                Task { await viewModel.remove(entry) }
            }
            .controlSize(.small)
        }
        .padding(.vertical, 2)
    }

    private func runsText(_ entry: AlwaysAllowEntry) -> String {
        let runs = entry.runs ?? 0
        return runs == 1 ? "Ran 1 time" : "Ran \(runs) times"
    }
}
