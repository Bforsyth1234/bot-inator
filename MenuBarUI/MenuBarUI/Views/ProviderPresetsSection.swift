import SwiftUI

/// One OpenAI-compatible provider preset as ``GET /api/settings`` lists it
/// (#97). The daemon never sends a full key, only whether one is saved and
/// its last 4 characters.
struct ProviderPreset: Codable, Equatable, Identifiable {
    var id: String
    var label: String
    var tested: Bool
    var docsUrl: String
    var keyRequired: Bool
    var keySet: Bool
    var keyHint: String
    var apiBase: String
    var defaultApiBase: String
    var cloud: Bool
    var models: [String: String]
    var defaultModels: [String: String]
    var inUse: [String]
    var problem: String
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case id, label, tested, cloud, models, problem
        case docsUrl = "docs_url"
        case keyRequired = "key_required"
        case keySet = "key_set"
        case keyHint = "key_hint"
        case apiBase = "api_base"
        case defaultApiBase = "default_api_base"
        case defaultModels = "default_models"
        case inUse = "in_use"
        case lockedFields = "locked_fields"
    }

    var host: String { URL(string: apiBase)?.host ?? apiBase }
}

/// Editable state for one preset in the Settings form.
struct PresetForm: Equatable {
    var apiKey = ""   // blank = keep the saved key
    var apiBase = ""
    var models: [String: String] = [:]
    var modelChoices: [String] = []
    var testResult: String?
}

let engineRoles = ["main", "analysis", "drafting"]
let engineRoleTitles = ["main": "Main", "analysis": "Analysis", "drafting": "Drafting"]

/// The settings for one preset that an engine uses: key, base URL, the
/// model for each role it serves, "List models" and "Test" (#97).
struct ProviderPresetSection: View {
    let preset: ProviderPreset
    let roles: [String]
    @Binding var form: PresetForm
    let listModels: () async -> Void
    let test: () async -> Void

    var body: some View {
        Section {
            if !preset.tested {
                Label("Untested: no recorded run with this provider yet. Use Test before relying on it.",
                      systemImage: "exclamationmark.circle")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if preset.keyRequired {
                SecureField(keyPlaceholder, text: $form.apiKey)
                    .disabled(preset.lockedFields.contains("api_key"))
            }
            TextField("Base URL", text: $form.apiBase, prompt: Text(preset.defaultApiBase))
                .disabled(preset.lockedFields.contains("api_base"))
            Text(destinationNote)
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            ForEach(roles, id: \.self) { role in
                modelRow(role)
            }
            HStack {
                Button("List models") { Task { await listModels() } }
                Button("Test") { Task { await test() } }
                if !preset.docsUrl.isEmpty, let url = URL(string: preset.docsUrl) {
                    Spacer()
                    Link("Docs", destination: url).appFont(.secondary)
                }
            }
            if let result = form.testResult {
                Text(result).appFont(.secondary).foregroundStyle(.secondary)
            }
            if !preset.problem.isEmpty {
                Label(preset.problem, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text(preset.label)
        }
    }

    @ViewBuilder
    private func modelRow(_ role: String) -> some View {
        let binding = Binding(
            get: { form.models[role] ?? "" },
            set: { form.models[role] = $0 }
        )
        HStack {
            TextField("\(engineRoleTitles[role] ?? role) model", text: binding,
                      prompt: Text(preset.defaultModels[role] ?? "pick one"))
                .disabled(preset.lockedFields.contains("\(role)_model"))
            if !form.modelChoices.isEmpty {
                Menu("Choose") {
                    ForEach(form.modelChoices, id: \.self) { id in
                        Button(id) { form.models[role] = id }
                    }
                }
                .fixedSize()
            }
        }
    }

    private var keyPlaceholder: String {
        preset.keySet ? "Saved key \(preset.keyHint) (type to replace)" : "API key"
    }

    private var destinationNote: String {
        let base = form.apiBase.isEmpty ? preset.defaultApiBase : form.apiBase
        let host = URL(string: base)?.host ?? base
        if host.isEmpty { return "Set the server's base URL, e.g. http://localhost:11434/v1." }
        let local = ["localhost", "127.0.0.1", "::1"].contains(host) || host.hasSuffix(".localhost")
        return local
            ? "Prompts go to \(host) on this Mac and stay here."
            : "Prompts for these engines are sent to \(host)."
    }
}
