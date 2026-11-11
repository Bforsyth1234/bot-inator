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
    // OpenAI-compatible presets (#97). Optional so an older daemon decodes.
    var presets: [ProviderPreset]?

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
        case presets
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
    @Published private(set) var cloudPersonalText = false
    // Per-preset form state (#97), keyed by preset id.
    @Published var presetForms: [String: PresetForm] = [:]

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

    /// Every provider the pickers offer: local plus the daemon's presets.
    var providerChoices: [(id: String, label: String)] {
        let presets = saved?.presets?.map { ($0.id, $0.label) } ?? [("groq", "Groq")]
        return [("local", "Local (MLX on this Mac)")] + presets
    }

    func providerLabel(_ id: String) -> String {
        providerChoices.first { $0.id == id }?.label ?? id
    }

    /// The provider each role runs on, as edited.
    var editedEngines: [String: String] {
        let overrides = ["main": providerMain, "analysis": providerAnalysis,
                         "drafting": providerDrafting]
        return overrides.mapValues { $0.isEmpty ? provider : $0 }
    }

    /// Presets an engine uses in the form, with the roles they serve.
    var presetsInUse: [(preset: ProviderPreset, roles: [String])] {
        let engines = editedEngines
        return (saved?.presets ?? []).compactMap { preset in
            let roles = engineRoles.filter { engines[$0] == preset.id }
            return roles.isEmpty ? nil : (preset, roles)
        }
    }

    private func isCloudProvider(_ id: String) -> Bool {
        if id == "local" { return false }
        return saved?.presets?.first { $0.id == id }?.cloud ?? true
    }

    /// True when the personal-text toggle matters: an engine is on a cloud
    /// provider in the form as edited, or in the running daemon (#52).
    var showsPersonalTextToggle: Bool {
        editedEngines.values.contains(where: isCloudProvider)
            || (saved?.runningEngineProviders?.values.contains(where: isCloudProvider) ?? false)
    }

    func form(_ id: String) -> Binding<PresetForm> {
        Binding(
            get: { self.presetForms[id] ?? PresetForm() },
            set: { self.presetForms[id] = $0 }
        )
    }

    /// The key / base typed but not saved yet, so List models and Test
    /// work before Save.
    private func probeBody(_ id: String, model: String? = nil) -> [String: String] {
        let f = presetForms[id] ?? PresetForm()
        var body: [String: String] = [:]
        let key = f.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if !key.isEmpty { body["api_key"] = key }
        let base = f.apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
        if !base.isEmpty { body["api_base"] = base }
        if let model, !model.isEmpty { body["model"] = model }
        return body
    }

    func listModels(_ id: String) async {
        do {
            let data = try await post("api/providers/\(id)/models", probeBody(id))
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let ids = obj?["models"] as? [String] ?? []
            presetForms[id, default: PresetForm()].modelChoices = ids
            presetForms[id, default: PresetForm()].testResult = ids.isEmpty
                ? "The provider listed no models."
                : "\(ids.count) models listed: use Choose next to each model."
        } catch {
            presetForms[id, default: PresetForm()].testResult = "List failed: \(error.localizedDescription)"
        }
    }

    func testPreset(_ id: String, role: String) async {
        let model = presetForms[id]?.models[role] ?? ""
        do {
            let data = try await post("api/providers/\(id)/test", probeBody(id, model: model))
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let ok = obj?["ok"] as? Bool ?? false
            let used = obj?["model"] as? String ?? model
            presetForms[id, default: PresetForm()].testResult = ok
                ? "Test passed: \(used) answered."
                : "Test failed (\(used)): \(obj?["error"] as? String ?? "unknown error")"
        } catch {
            presetForms[id, default: PresetForm()].testResult = "Test failed: \(error.localizedDescription)"
        }
    }

    private func post(_ path: String, _ body: [String: String]) async throws -> Data {
        var request = try DaemonAuth.request(baseURL.appendingPathComponent(path), method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        isBusy = true
        defer { isBusy = false }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        try Self.checkStatus(response, data)
        return data
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
        var body: [String: Any] = [
            "provider": provider,
            "provider_main": providerMain,
            "provider_analysis": providerAnalysis,
            "provider_drafting": providerDrafting,
        ]
        // Presets an engine uses (#97). Groq is one; its settings are the
        // same keys the old groq_* fields wrote.
        var presetSettings: [String: [String: String]] = [:]
        for (preset, roles) in presetsInUse {
            let f = presetForms[preset.id] ?? PresetForm()
            var fields: [String: String] = [
                "api_base": f.apiBase.trimmingCharacters(in: .whitespacesAndNewlines),
            ]
            for role in roles {
                fields["\(role)_model"] = (f.models[role] ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            // Blank field means "keep the saved key".
            let key = f.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
            if !key.isEmpty { fields["api_key"] = key }
            presetSettings[preset.id] = fields.filter { !preset.lockedFields.contains($0.key) }
        }
        if !presetSettings.isEmpty { body["preset_settings"] = presetSettings }

        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/settings"), method: "PUT"
            )
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(DaemonSettings.self, from: data))
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
        cloudPersonalText = s.cloudPersonalText ?? false
        // Fresh form per preset from what is saved; typed keys are dropped
        // (never kept after a save), fetched model lists are kept.
        for preset in s.presets ?? [] {
            var f = PresetForm()
            f.apiBase = preset.apiBase == preset.defaultApiBase ? "" : preset.apiBase
            f.models = preset.models
            f.modelChoices = presetForms[preset.id]?.modelChoices ?? []
            presetForms[preset.id] = f
        }
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
            // #96: bundled daemon, start at login, new-version check.
            AppLifecycleSettingsView()

            Section("Inference provider") {
                Picker("Provider", selection: $viewModel.provider) {
                    ForEach(viewModel.providerChoices, id: \.id) { choice in
                        Text(choice.label).tag(choice.id)
                    }
                }
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

            ForEach(viewModel.presetsInUse, id: \.preset.id) { item in
                ProviderPresetSection(
                    preset: item.preset, roles: item.roles,
                    form: viewModel.form(item.preset.id),
                    listModels: { await viewModel.listModels(item.preset.id) },
                    test: { await viewModel.testPreset(item.preset.id, role: item.roles[0]) }
                )
            }

            DailyBriefSettingsView()
            MeetingPrepSettingsView()
            // Wave 16: meeting notes (#86), "Watch me" shortcut (#84),
            // iPhone remote (#85).
            MeetingNotesSettingsView()
            QuickAskSettingsView()
            RecordShortcutSettingsView()
            RemoteSettingsView()
            ActivityHistorySettingsView()
            // #107: which model look_at_screen asks.
            VisionSettingsView()
            PermissionsView()

            // #78: "always do this" rules the user turned on.
            Section("Standing rules") {
                RulesView()
            }

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
             ? "Cloud providers receive iMessage text and email subjects and bodies. Applies to the next prompt."
             : "iMessage text and email subjects and bodies are replaced by a placeholder before a prompt goes to a cloud provider.")
            .appFont(.secondary)
            .foregroundStyle(.secondary)
        lockedNote("cloud_personal_text", env: "AGENT_CLOUD_PERSONAL_TEXT")
    }

    @ViewBuilder
    private func enginePicker(_ title: String, field: String, env: String,
                              selection: Binding<String>) -> some View {
        Picker(title, selection: selection) {
            Text("Same as default (\(viewModel.providerLabel(viewModel.provider)))").tag("")
            ForEach(viewModel.providerChoices, id: \.id) { choice in
                Text(choice.label).tag(choice.id)
            }
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


/// #84: the "Watch me" record hotkey (default ⌃⌥⌘R).
struct RecordShortcutSettingsView: View {
    @AppStorage(RecordShortcut.storageKey) private var stored: String = RecordShortcut.fallback.rawValue
    @ObservedObject private var hotKeys = RecordHotKeyController.shared

    var body: some View {
        Section("Watch me") {
            Picker("Record shortcut", selection: $stored) {
                ForEach(RecordShortcut.allCases) { shortcut in
                    Text(shortcut.label).tag(shortcut.rawValue)
                }
            }
            Text("Starts and stops a recording of one task (at most 5 minutes). Only file, app and window names are noted, never keystrokes; you review the steps before anything is drafted.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = hotKeys.lastError {
                HStack {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .appFont(.secondary)
                    Spacer()
                    Button("Retry") { hotKeys.retry() }
                }
            }
        }
    }
}
