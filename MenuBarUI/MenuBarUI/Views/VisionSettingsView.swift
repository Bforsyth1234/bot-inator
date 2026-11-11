import SwiftUI

/// Response of `GET`/`PUT /api/providers/vision` (#107).
struct VisionSettings: Codable, Equatable {
    struct LocalModel: Codable, Equatable, Identifiable {
        var id: String
        var bytes: Int
        var cached: Bool
    }
    struct PresetRow: Codable, Equatable, Identifiable {
        var id: String
        var label: String
        var cloud: Bool
    }
    struct RouteInfo: Codable, Equatable {
        var kind: String
        var model: String
        var host: String
        var refused: String
    }

    var provider: String
    var localModel: String
    var defaultLocalModel: String
    var localModels: [LocalModel]
    var mlxVlmInstalled: Bool
    var cloudModels: [String: String]
    var presets: [PresetRow]
    var cloudPersonalText: Bool
    var route: RouteInfo
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case provider, presets, route
        case localModel = "local_model"
        case defaultLocalModel = "default_local_model"
        case localModels = "local_models"
        case mlxVlmInstalled = "mlx_vlm_installed"
        case cloudModels = "cloud_models"
        case cloudPersonalText = "cloud_personal_text"
        case lockedFields = "locked_fields"
    }
}

private struct VisionUpdate: Encodable {
    var provider: String?
    var localModel: String?
    var cloudModel: String?

    enum CodingKeys: String, CodingKey {
        case provider
        case localModel = "local_model"
        case cloudModel = "cloud_model"
    }
}

@MainActor
final class VisionSettingsViewModel: ObservableObject {
    @Published private(set) var settings: VisionSettings?
    @Published var cloudModelDraft = ""
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func isLocked(_ field: String) -> Bool { settings?.lockedFields.contains(field) ?? false }

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    func setProvider(_ value: String) async {
        await send(method: "PUT", body: VisionUpdate(provider: value), status: nil)
    }

    func setLocalModel(_ value: String) async {
        await send(method: "PUT", body: VisionUpdate(localModel: value), status: nil)
    }

    /// The same opt-in as the engine settings' personal-text toggle (#52):
    /// `PUT /api/settings {"cloud_personal_text": …}`, then re-read.
    func setPersonalText(_ value: Bool) async {
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/settings"), method: "PUT"
            )
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(["cloud_personal_text": value])
            let (_, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw NSError(domain: "VisionSettings", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
            }
        } catch {
            errorMessage = "Save failed: " + error.localizedDescription
            return
        }
        await refresh()
    }

    func saveCloudModel() async {
        await send(method: "PUT", body: VisionUpdate(cloudModel: cloudModelDraft),
                   status: "Vision model saved.")
    }

    private func send(method: String, body: VisionUpdate?, status: String?) async {
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/providers/vision"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
                throw NSError(domain: "VisionSettings", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"])
            }
            let decoded = try JSONDecoder().decode(VisionSettings.self, from: data)
            settings = decoded
            cloudModelDraft = decoded.cloudModels[decoded.provider] ?? ""
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (method == "GET" ? "" : "Save failed: ") + error.localizedDescription
        }
    }
}

/// "Vision" section for Settings (#107): which model `look_at_screen` asks.
struct VisionSettingsView: View {
    @StateObject private var viewModel = VisionSettingsViewModel()

    var body: some View {
        Section {
            if let s = viewModel.settings {
                Picker("Vision model runs on", selection: Binding(
                    get: { s.provider },
                    set: { value in Task { await viewModel.setProvider(value) } }
                )) {
                    Text("This Mac (local)").tag("local")
                    ForEach(s.presets) { preset in
                        Text(preset.cloud ? preset.label : "\(preset.label) (on this Mac)")
                            .tag(preset.id)
                    }
                }
                .disabled(viewModel.isLocked("provider"))

                if s.provider == "local" {
                    localRows(s)
                } else {
                    cloudRows(s)
                }
                routeLine(s)
            } else if let error = viewModel.errorMessage, !error.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            } else {
                Text("Loading…").appFont(.secondary).foregroundStyle(.secondary)
            }
            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if viewModel.settings != nil, let error = viewModel.errorMessage, !error.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Vision (look at my screen)")
        } footer: {
            Text("When you ask about what's on screen, the agent asks first, captures the "
                 + "front window once, asks the vision model, and deletes the image. "
                 + "Password managers and password fields are never captured.")
                .appFont(.secondary)
        }
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func localRows(_ s: VisionSettings) -> some View {
        Picker("Model", selection: Binding(
            get: { s.localModel },
            set: { value in Task { await viewModel.setLocalModel(value) } }
        )) {
            ForEach(s.localModels) { model in
                Text(localLabel(model, isDefault: model.id == s.defaultLocalModel)).tag(model.id)
            }
            if !s.localModels.contains(where: { $0.id == s.localModel }) {
                Text(s.localModel).tag(s.localModel)
            }
        }
        .disabled(viewModel.isLocked("local_model"))
        Text("Loads on the first question (not at start) and unloads after 10 idle minutes. "
             + "The 4B model needs about 4.5 GB of memory while loaded, the 2B about 3.4 GB.")
            .appFont(.secondary)
            .foregroundStyle(.secondary)
        if !s.mlxVlmInstalled {
            Label("The mlx-vlm package isn't installed in the daemon's Python, so the local "
                  + "vision model can't run. Reinstall the requirements, then restart.",
                  systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        }
    }

    @ViewBuilder
    private func cloudRows(_ s: VisionSettings) -> some View {
        HStack {
            TextField("Image-capable model id", text: $viewModel.cloudModelDraft)
                .textFieldStyle(.roundedBorder)
                .disabled(viewModel.isLocked("cloud_model"))
            Button("Save") { Task { await viewModel.saveCloudModel() } }
                .disabled(viewModel.isLocked("cloud_model")
                          || viewModel.cloudModelDraft == (s.cloudModels[s.provider] ?? ""))
        }
        Toggle("Let cloud models see email and message text, and screenshots", isOn: Binding(
            get: { s.cloudPersonalText },
            set: { value in Task { await viewModel.setPersonalText(value) } }
        ))
        Text("Uses this provider's API key and base URL from the engine settings above. "
             + "A screenshot shows mail, messages and more, so it takes the same "
             + "permission as personal text; it also applies to the cloud text engines.")
            .appFont(.secondary)
            .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private func routeLine(_ s: VisionSettings) -> some View {
        if !s.route.refused.isEmpty {
            Label(s.route.refused, systemImage: "hand.raised.fill")
                .foregroundStyle(.orange)
                .appFont(.secondary)
        } else if s.route.kind == "cloud" {
            Label("Screenshots go to \(s.route.host.isEmpty ? "the provider" : s.route.host) "
                  + "(\(s.route.model)) and are listed in the privacy ledger.",
                  systemImage: "icloud.and.arrow.up")
                .appFont(.secondary)
        } else {
            Label("Screenshots stay on this Mac.", systemImage: "lock.fill")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        }
    }

    private func localLabel(_ model: VisionSettings.LocalModel, isDefault: Bool) -> String {
        let name = model.id.split(separator: "/").last.map(String.init) ?? model.id
        let size = model.bytes > 0
            ? String(format: " · %.1f GB", Double(model.bytes) / 1_000_000_000) : ""
        let state = model.cached ? "" : " · downloads on first use"
        return name + size + state + (isDefault ? " (recommended for this Mac)" : "")
    }
}
