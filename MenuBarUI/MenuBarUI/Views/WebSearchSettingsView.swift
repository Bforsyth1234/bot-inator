import SwiftUI

/// Response of `GET`/`PUT /api/web-search` (#124). The key itself is never
/// returned, only whether one is saved.
private struct WebSearchSettings: Codable, Equatable {
    var enabled: Bool
    var provider: String        // "brave" or "none"
    var hasBraveKey: Bool
    var noKeyProvider: Bool
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, provider
        case hasBraveKey = "has_brave_key"
        case noKeyProvider = "no_key_provider"
        case lockedFields = "locked_fields"
    }
}

private struct WebSearchUpdate: Encodable {
    var enabled: Bool?
    var braveKey: String?

    enum CodingKeys: String, CodingKey {
        case enabled
        case braveKey = "brave_key"
    }
}

@MainActor
final class WebSearchSettingsViewModel: ObservableObject {
    @Published private(set) var enabled = false
    @Published private(set) var provider = "none"
    @Published private(set) var hasBraveKey = false
    @Published private(set) var noKeyProvider = false
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var loaded = false
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func isLocked(_ field: String) -> Bool { lockedFields.contains(field) }

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    /// Saved as soon as it is flipped: the tools read it on every call.
    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: WebSearchUpdate(enabled: value, braveKey: nil),
                   status: value ? "Web search is on." : "Web search is off.")
    }

    func saveKey(_ key: String) async {
        await send(method: "PUT", body: WebSearchUpdate(enabled: nil, braveKey: key),
                   status: "Brave key saved.")
    }

    func removeKey() async {
        await send(method: "PUT", body: WebSearchUpdate(enabled: nil, braveKey: ""),
                   status: "Brave key removed.")
    }

    private func send(method: String, body: WebSearchUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/web-search"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(WebSearchSettings.self, from: data))
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (body == nil ? "" : "Save failed: ") + error.localizedDescription
            if body != nil { await reload() }
        }
    }

    /// After a failed save, show what the daemon actually has.
    private func reload() async {
        guard let request = try? DaemonAuth.request(
            baseURL.appendingPathComponent("api/web-search")
        ), let (data, _) = try? await session.data(for: request),
        let settings = try? JSONDecoder().decode(WebSearchSettings.self, from: data)
        else { return }
        apply(settings)
    }

    private func apply(_ settings: WebSearchSettings) {
        enabled = settings.enabled
        provider = settings.provider
        hasBraveKey = settings.hasBraveKey
        noKeyProvider = settings.noKeyProvider
        lockedFields = settings.lockedFields
        loaded = true
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "WebSearch", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "Web search" section for Settings (#124). Embed inside a `Form` with one
/// line: `WebSearchSettingsView()`.
struct WebSearchSettingsView: View {
    @StateObject private var viewModel = WebSearchSettingsViewModel()
    @State private var draftKey = ""

    var body: some View {
        Section {
            Toggle("Let Chat search the web", isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_WEB_SEARCH")

            Text("Off by default. When on, your search words and the addresses of pages it reads leave this Mac; "
                 + "each request is listed in the Privacy ledger. It runs without asking only when you ask in Chat "
                 + "and nothing private was read first; otherwise you get a card showing exactly what would be sent.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)

            LabeledContent("Search provider") {
                Text(providerText).appFont(.secondary)
            }

            if viewModel.hasBraveKey {
                HStack {
                    Label("Brave key saved", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                    Spacer()
                    Button("Remove") { Task { await viewModel.removeKey() } }
                        .disabled(viewModel.isBusy || viewModel.isLocked("brave_key"))
                }
            } else {
                HStack {
                    SecureField("Brave Search API key", text: $draftKey)
                        .textFieldStyle(.roundedBorder)
                    Button("Save") {
                        let key = draftKey.trimmingCharacters(in: .whitespacesAndNewlines)
                        draftKey = ""
                        Task { await viewModel.saveKey(key) }
                    }
                    .disabled(draftKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                              || viewModel.isBusy || viewModel.isLocked("brave_key"))
                }
                Text("Searching needs your own key from brave.com/search/api. Brave asks for a credit card when you sign up. "
                     + "Reading a page you name works without a key.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            lockedNote("brave_key", env: "AGENT_BRAVE_API_KEY")

            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Web search")
        }
        .task { await viewModel.refresh() }
    }

    private var providerText: String {
        if viewModel.provider == "brave" { return "Brave (your key)" }
        if viewModel.noKeyProvider { return "\(viewModel.provider) (no key; can stop working without notice)" }
        return "None yet: add a Brave key"
    }

    @ViewBuilder
    private func lockedNote(_ field: String, env: String) -> some View {
        if viewModel.isLocked(field) {
            Text("Set by \(env) in the daemon's shell; unset it there to change it here.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        }
    }
}
