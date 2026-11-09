import SwiftUI

/// Response of ``GET``/``PUT /api/settings``. The daemon never sends the
/// full API key back, only whether one is saved and its last 4 characters.
struct DaemonSettings: Codable, Equatable {
    var provider: String
    var runningProvider: String
    var groqApiKeySet: Bool
    var groqApiKeyHint: String
    var groqMainModel: String
    var groqAnalysisModel: String
    var groqDraftingModel: String
    var restartRequired: Bool
    var canRestart: Bool
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case provider
        case runningProvider = "running_provider"
        case groqApiKeySet = "groq_api_key_set"
        case groqApiKeyHint = "groq_api_key_hint"
        case groqMainModel = "groq_main_model"
        case groqAnalysisModel = "groq_analysis_model"
        case groqDraftingModel = "groq_drafting_model"
        case restartRequired = "restart_required"
        case canRestart = "can_restart"
        case lockedFields = "locked_fields"
    }
}

@MainActor
final class SettingsViewModel: ObservableObject {
    @Published private(set) var saved: DaemonSettings?
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    // Editable form state.
    @Published var provider = "local"
    @Published var apiKey = ""
    @Published var mainModel = ""
    @Published var analysisModel = ""
    @Published var draftingModel = ""

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func isLocked(_ field: String) -> Bool {
        saved?.lockedFields.contains(field) ?? false
    }

    func refresh() async {
        isBusy = true
        defer { isBusy = false }
        do {
            let (data, response) = try await session.data(
                from: baseURL.appendingPathComponent("api/settings")
            )
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(DaemonSettings.self, from: data))
            errorMessage = nil
        } catch {
            errorMessage = "Can't reach the daemon: \(error.localizedDescription)"
        }
    }

    func save() async {
        var body: [String: String] = [
            "provider": provider,
            "groq_main_model": mainModel,
            "groq_analysis_model": analysisModel,
            "groq_drafting_model": draftingModel,
        ]
        // Blank field means "keep the saved key".
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !key.isEmpty { body["groq_api_key"] = key }

        var request = URLRequest(url: baseURL.appendingPathComponent("api/settings"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        isBusy = true
        defer { isBusy = false }
        do {
            request.httpBody = try JSONEncoder().encode(body)
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(DaemonSettings.self, from: data))
            apiKey = ""
            errorMessage = nil
            statusMessage = saved?.restartRequired == true
                ? "Saved. Restart the daemon to apply."
                : "Saved."
        } catch {
            errorMessage = "Save failed: \(error.localizedDescription)"
        }
    }

    func restart() async {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/restart"))
        request.httpMethod = "POST"
        isBusy = true
        defer { isBusy = false }
        do {
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response, data)
        } catch {
            errorMessage = "Restart failed: \(error.localizedDescription)"
            return
        }
        statusMessage = "Restarting…"
        // Local MLX models can take a while to load; poll for up to ~60s.
        for _ in 0..<60 {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if await healthy() {
                await refresh()
                statusMessage = "Daemon restarted on \(saved?.runningProvider ?? provider)."
                return
            }
        }
        errorMessage = "Daemon didn't come back after 60s. Check its terminal."
        statusMessage = nil
    }

    private func healthy() async -> Bool {
        guard let (_, response) = try? await session.data(
            from: baseURL.appendingPathComponent("health")
        ) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    private func apply(_ s: DaemonSettings) {
        saved = s
        provider = s.provider
        mainModel = s.groqMainModel
        analysisModel = s.groqAnalysisModel
        draftingModel = s.groqDraftingModel
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        // FastAPI errors look like {"detail": "..."}.
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "Settings", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

struct SettingsView: View {
    @StateObject private var viewModel = SettingsViewModel()

    var body: some View {
        Form {
            Section("Inference provider") {
                Picker("Provider", selection: $viewModel.provider) {
                    Text("Local (MLX on this Mac)").tag("local")
                    Text("Groq (cloud)").tag("groq")
                }
                .pickerStyle(.radioGroup)
                .disabled(viewModel.isLocked("provider"))
                if let saved = viewModel.saved {
                    Text("Running now: \(saved.runningProvider)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                lockedNote("provider", env: "AGENT_PROVIDER")
            }

            if viewModel.provider == "groq" {
                Section("Groq") {
                    SecureField(apiKeyPlaceholder, text: $viewModel.apiKey)
                        .disabled(viewModel.isLocked("groq_api_key"))
                    Link("Get a key at console.groq.com/keys",
                         destination: URL(string: "https://console.groq.com/keys")!)
                        .font(.caption)
                    lockedNote("groq_api_key", env: "GROQ_API_KEY")
                    DisclosureGroup("Models") {
                        TextField("Main", text: $viewModel.mainModel)
                            .disabled(viewModel.isLocked("groq_main_model"))
                        TextField("Analysis", text: $viewModel.analysisModel)
                            .disabled(viewModel.isLocked("groq_analysis_model"))
                        TextField("Drafting", text: $viewModel.draftingModel)
                            .disabled(viewModel.isLocked("groq_drafting_model"))
                    }
                }
            }

            Section {
                if let error = viewModel.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }
                if let status = viewModel.statusMessage {
                    Text(status).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Save") { Task { await viewModel.save() } }
                        .keyboardShortcut(.defaultAction)
                    if viewModel.saved?.restartRequired == true {
                        Button("Restart Daemon") { Task { await viewModel.restart() } }
                            .disabled(viewModel.saved?.canRestart != true)
                            .help(viewModel.saved?.canRestart == true
                                  ? "Restart the daemon so the saved settings take effect"
                                  : "Daemon runs with --reload; restart it from its terminal")
                    }
                    Spacer()
                    if viewModel.isBusy { ProgressView().controlSize(.small) }
                }
            }
        }
        .formStyle(.grouped)
        .disabled(viewModel.isBusy)
        .frame(minWidth: 440, minHeight: 360)
        .task { await viewModel.refresh() }
    }

    private var apiKeyPlaceholder: String {
        guard let saved = viewModel.saved, saved.groqApiKeySet else { return "API key" }
        return "Saved key \(saved.groqApiKeyHint) (type to replace)"
    }

    @ViewBuilder
    private func lockedNote(_ field: String, env: String) -> some View {
        if viewModel.isLocked(field) {
            Text("Set by \(env) in the daemon's shell; unset it there to change it here.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}
