import SwiftUI

/// Response of ``GET``/``PUT /api/settings``. The daemon never sends the
/// full API key back, only whether one is saved and its last 4 characters.
struct DaemonSettings: Codable, Equatable {
    var provider: String
    var runningProvider: String
    // Per-engine overrides (#26): "" means "same as provider". Optional so
    // an older daemon without them still decodes.
    var providerMain: String?
    var providerAnalysis: String?
    var providerDrafting: String?
    var runningEngineProviders: [String: String]?
    var groqApiKeyRequired: Bool?
    var groqApiKeySet: Bool
    var groqApiKeyHint: String
    var groqMainModel: String
    var groqAnalysisModel: String
    var groqDraftingModel: String
    var restartRequired: Bool
    var canRestart: Bool
    var lockedFields: [String]
    // Let cloud engines see email and message text (#52). Applies live.
    // Optional so an older daemon without it still decodes.
    var cloudPersonalText: Bool?

    enum CodingKeys: String, CodingKey {
        case provider
        case runningProvider = "running_provider"
        case providerMain = "provider_main"
        case providerAnalysis = "provider_analysis"
        case providerDrafting = "provider_drafting"
        case runningEngineProviders = "running_engine_providers"
        case groqApiKeyRequired = "groq_api_key_required"
        case groqApiKeySet = "groq_api_key_set"
        case groqApiKeyHint = "groq_api_key_hint"
        case groqMainModel = "groq_main_model"
        case groqAnalysisModel = "groq_analysis_model"
        case groqDraftingModel = "groq_drafting_model"
        case restartRequired = "restart_required"
        case canRestart = "can_restart"
        case lockedFields = "locked_fields"
        case cloudPersonalText = "cloud_personal_text"
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
    @Published var providerMain = ""
    @Published var providerAnalysis = ""
    @Published var providerDrafting = ""
    @Published var apiKey = ""
    @Published var mainModel = ""
    @Published var analysisModel = ""
    @Published var draftingModel = ""
    @Published private(set) var cloudPersonalText = false

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

    /// True when the form, as edited, puts any engine on Groq.
    var usesGroq: Bool {
        [providerMain, providerAnalysis, providerDrafting]
            .map { $0.isEmpty ? provider : $0 }
            .contains("groq")
    }

    /// True when the personal-text toggle matters: an engine uses Groq in
    /// the form as edited, or in the running daemon (#52).
    var showsPersonalTextToggle: Bool {
        usesGroq || (saved?.runningEngineProviders?.values.contains("groq") ?? false)
            || saved?.runningProvider == "groq"
    }

    /// Save the personal-text toggle at once: the daemon reads it on every
    /// prompt, so it needs neither Save nor a restart (#52).
    func setCloudPersonalText(_ value: Bool) async {
        cloudPersonalText = value
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/settings"), method: "PUT"
            )
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(["cloud_personal_text": value])
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            let s = try JSONDecoder().decode(DaemonSettings.self, from: data)
            saved = s
            cloudPersonalText = s.cloudPersonalText ?? false
            errorMessage = nil
            statusMessage = cloudPersonalText
                ? "Cloud models now see email and message text."
                : "Email and message text now stays on this Mac."
        } catch {
            cloudPersonalText = saved?.cloudPersonalText ?? false
            errorMessage = "Save failed: \(error.localizedDescription)"
        }
    }

    /// "Running now" line, per engine when they differ.
    var runningSummary: String? {
        guard let saved else { return nil }
        guard let engines = saved.runningEngineProviders,
              Set(engines.values).count > 1 else { return saved.runningProvider }
        return ["main", "analysis", "drafting"]
            .map { "\($0) \(engines[$0] ?? saved.runningProvider)" }
            .joined(separator: ", ")
    }

    func refresh() async {
        isBusy = true
        defer { isBusy = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/settings")
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
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
            "provider_main": providerMain,
            "provider_analysis": providerAnalysis,
            "provider_drafting": providerDrafting,
            "groq_main_model": mainModel,
            "groq_analysis_model": analysisModel,
            "groq_drafting_model": draftingModel,
        ]
        // Blank field means "keep the saved key".
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !key.isEmpty { body["groq_api_key"] = key }

        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/settings"), method: "PUT"
            )
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
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
        isBusy = true
        defer { isBusy = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/restart"), method: "POST"
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
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
        providerMain = s.providerMain ?? ""
        providerAnalysis = s.providerAnalysis ?? ""
        providerDrafting = s.providerDrafting ?? ""
        mainModel = s.groqMainModel
        analysisModel = s.groqAnalysisModel
        draftingModel = s.groqDraftingModel
        cloudPersonalText = s.cloudPersonalText ?? false
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
    /// #87: shared with the menu and the Inbox (MenuBarUIApp).
    @EnvironmentObject private var delivery: DeliveryViewModel

    var body: some View {
        Form {
            Section("Appearance") {
                TextSizePicker()
            }

            Section("Inference provider") {
                Picker("Provider", selection: $viewModel.provider) {
                    Text("Local (MLX on this Mac)").tag("local")
                    Text("Groq (cloud)").tag("groq")
                }
                .pickerStyle(.radioGroup)
                .disabled(viewModel.isLocked("provider"))
                if let running = viewModel.runningSummary {
                    Text("Running now: \(running)")
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                }
                lockedNote("provider", env: "AGENT_PROVIDER")
                DisclosureGroup("Per engine") {
                    enginePicker("Main (agent)", field: "provider_main",
                                 env: "AGENT_PROVIDER_MAIN",
                                 selection: $viewModel.providerMain)
                    enginePicker("Analysis", field: "provider_analysis",
                                 env: "AGENT_PROVIDER_ANALYSIS",
                                 selection: $viewModel.providerAnalysis)
                    enginePicker("Drafting (new tools)", field: "provider_drafting",
                                 env: "AGENT_PROVIDER_DRAFTING",
                                 selection: $viewModel.providerDrafting)
                }
                if viewModel.showsPersonalTextToggle {
                    personalTextToggle
                }
            }

            if viewModel.usesGroq {
                Section("Groq") {
                    SecureField(apiKeyPlaceholder, text: $viewModel.apiKey)
                        .disabled(viewModel.isLocked("groq_api_key"))
                    Link("Get a key at console.groq.com/keys",
                         destination: URL(string: "https://console.groq.com/keys")!)
                        .appFont(.secondary)
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

            DailyBriefSettingsView()
            MeetingPrepSettingsView()
            QuickAskSettingsView()
            ActivityHistorySettingsView()
            PermissionsView()

            // Wave 15: watches (#88), right-moment delivery (#87), what the
            // assistant did with Undo (#83), what left this Mac (#82).
            Section("Watch for…") {
                WatchesView()
            }
            Section("Delivery") {
                DeliverySettingsView(viewModel: delivery)
            }
            Section("History") {
                ActionHistoryView()
            }
            PrivacyLedgerView()

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

    /// Off by default (invariant 7c). Saved as soon as it is flipped.
    @ViewBuilder
    private var personalTextToggle: some View {
        Toggle("Let cloud models see email and message text", isOn: Binding(
            get: { viewModel.cloudPersonalText },
            set: { value in Task { await viewModel.setCloudPersonalText(value) } }
        ))
        .disabled(viewModel.isLocked("cloud_personal_text"))
        Text(viewModel.cloudPersonalText
             ? "Groq receives iMessage text and email subjects and bodies. Applies to the next prompt."
             : "iMessage text and email subjects and bodies are replaced by a placeholder before a prompt goes to Groq.")
            .appFont(.secondary)
            .foregroundStyle(.secondary)
        lockedNote("cloud_personal_text", env: "AGENT_CLOUD_PERSONAL_TEXT")
    }

    private var apiKeyPlaceholder: String {
        guard let saved = viewModel.saved, saved.groqApiKeySet else { return "API key" }
        return "Saved key \(saved.groqApiKeyHint) (type to replace)"
    }

    @ViewBuilder
    private func enginePicker(_ title: String, field: String, env: String,
                              selection: Binding<String>) -> some View {
        Picker(title, selection: selection) {
            Text("Same as default (\(viewModel.provider == "groq" ? "Groq" : "Local"))").tag("")
            Text("Local").tag("local")
            Text("Groq (cloud)").tag("groq")
        }
        .disabled(viewModel.isLocked(field))
        lockedNote(field, env: env)
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
