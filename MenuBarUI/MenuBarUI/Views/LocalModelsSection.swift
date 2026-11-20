import AppKit
import SwiftUI

/// One engine's local MLX model as ``GET /api/settings`` reports it under
/// `mlx_models` (#136).
struct LocalModelInfo: Codable, Equatable {
    var key: String            // MLX_MAIN_MODEL, ...
    var value: String          // what the next start uses; "" = `defaultModel`
    var defaultModel: String   // the default for this Mac's memory
    var source: String         // "shell" | "settings" | "env_file" | "default"
    var locked: Bool           // exported in the daemon's shell
    var local: Bool            // the role runs on MLX after the next start
    var running: String?       // configured in the running daemon
    var loaded: String?        // actually loaded ("" while nothing is)
    var loadError: String?     // why the configured model did not load

    enum CodingKeys: String, CodingKey {
        case key, value, source, locked, local, running, loaded
        case defaultModel = "default"
        case loadError = "load_error"
    }
}

/// Editable state for the "Local models" block. Owned by
/// `SettingsViewModel` so the single Save sends it: `load` from each
/// settings response, `pendingChanges` merged into the PUT body.
@MainActor
final class LocalModelsForm: ObservableObject {
    @Published private(set) var info: [String: LocalModelInfo] = [:]
    @Published private(set) var memoryNote = ""
    /// Typed text per role ("" = use the default).
    @Published var text: [String: String] = [:]

    func load(_ models: [String: LocalModelInfo]?, memoryNote note: String?) {
        info = models ?? [:]
        text = info.mapValues(\.value)
        memoryNote = note ?? ""
    }

    /// `mlx_<role>_model` for every unlocked field whose text differs from
    /// what is saved. "" asks the daemon to remove the saved value.
    func pendingChanges() -> [String: String] {
        var out: [String: String] = [:]
        for role in engineRoles {
            guard let i = info[role], !i.locked else { continue }
            let typed = (text[role] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if typed != i.value { out["mlx_\(role)_model"] = typed }
        }
        return out
    }

    func binding(_ role: String) -> Binding<String> {
        Binding(get: { self.text[role] ?? "" }, set: { self.text[role] = $0 })
    }

    func chooseFolder(_ role: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose a folder that holds an MLX model (config.json and .safetensors files)"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        text[role] = url.path
    }
}

/// "Local models" (#136): the MLX model for each engine that runs on this
/// Mac, as edited in the provider pickers (`engines`: role -> provider).
struct LocalModelsSection: View {
    @ObservedObject var form: LocalModelsForm
    let engines: [String: String]

    private var roles: [String] {
        engineRoles.filter { engines[$0] == "local" && form.info[$0] != nil }
    }

    var body: some View {
        if !roles.isEmpty {
            Section {
                ForEach(roles, id: \.self) { role in
                    if let info = form.info[role] {
                        row(role, info)
                    }
                }
                Text("A Hugging Face repo id (e.g. mlx-community/…) or a model folder on this Mac. Empty uses the default for this Mac's memory. A new repo id is downloaded from Hugging Face on the next start and listed in the privacy ledger. Applies after a restart.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !form.memoryNote.isEmpty {
                    Label(form.memoryNote, systemImage: "memorychip")
                        .appFont(.secondary)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } header: {
                Text("Local models")
            }
        }
    }

    @ViewBuilder
    private func row(_ role: String, _ info: LocalModelInfo) -> some View {
        HStack {
            TextField("\(engineRoleTitles[role] ?? role) model", text: form.binding(role),
                      prompt: Text(info.defaultModel))
                .disabled(info.locked)
            Button("Choose folder…") { form.chooseFolder(role) }
                .disabled(info.locked)
                .fixedSize()
        }
        if info.locked {
            // Same wording as SettingsView's lockedNote.
            Text("Set by \(info.key) in the daemon's shell; unset it there to change it here.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else if info.source == "env_file" {
            Text("Set by \(info.key) in the daemon's .env file. Clearing this field goes back to that value, not the default.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let error = info.loadError, !error.isEmpty {
            Label(error, systemImage: "exclamationmark.triangle.fill")
                .appFont(.secondary)
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
