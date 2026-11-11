import AppKit
import SwiftUI

// First-run engine choice (#93). Shown once, on an install with no
// ~/.bot-inator/settings.json. It marks one choice "Recommended for this
// Mac" from physical memory, but never selects one: the user picks, and
// nothing reaches a cloud engine until they have chosen it and pasted
// their own key (invariant 7c).

struct FirstRunChoice: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var leavesMac: String
    enum CodingKeys: String, CodingKey {
        case id, title
        case leavesMac = "leaves_mac"
    }
}

struct FirstRunPreset: Codable, Identifiable, Equatable {
    var id: String
    var label: String
    var tested: Bool
    var keyRequired: Bool
    var apiBase: String
    var defaultModels: [String: String]
    var docsUrl: String
    var note: String
    enum CodingKeys: String, CodingKey {
        case id, label, tested, note
        case keyRequired = "key_required"
        case apiBase = "api_base"
        case defaultModels = "default_models"
        case docsUrl = "docs_url"
    }
}

struct FirstRunDownload: Codable, Equatable {
    var total: Int
    var done: Int
    var running: Bool?
    var finished: Bool?
    var error: String?
}

struct FirstRunLocal: Codable, Equatable {
    var models: [String: String]
    var downloaded: [String: Bool]
    var download: FirstRunDownload
    var serverBase: String
    enum CodingKeys: String, CodingKey {
        case models, downloaded, download
        case serverBase = "server_base"
    }
}

struct FirstRunState: Codable, Equatable {
    struct Recommendation: Codable, Equatable {
        var choice: String
        var reason: String
    }
    var pending: Bool
    var memoryGb: Double?
    var recommended: Recommendation
    var choices: [FirstRunChoice]
    var ledgerLine: String
    var presets: [FirstRunPreset]
    var local: FirstRunLocal
    var download: FirstRunDownload
    enum CodingKeys: String, CodingKey {
        case pending, recommended, choices, presets, local, download
        case memoryGb = "memory_gb"
        case ledgerLine = "ledger_line"
    }
}

@MainActor
final class FirstRunViewModel: ObservableObject {
    @Published var state: FirstRunState?
    /// Starts empty: the recommendation is a badge, never a selection.
    @Published var choice: String?
    @Published var localSource = "download"
    @Published var folders: [String: String] = [:]
    @Published var serverBase = ""
    @Published var serverModels: [String: String] = [:]
    @Published var provider = ""
    @Published var apiKey = ""
    @Published var models: [String: String] = [:]
    @Published var modelChoices: [String] = []
    @Published var message: String?
    @Published var error: String?
    @Published var busy = false
    @Published var done = false

    private let baseURL = URL(string: "http://127.0.0.1:8000")!
    private let session = URLSession.shared

    var needsLocal: Bool { choice == "local" || choice == "mixed" }
    var needsCloud: Bool { choice == "cloud" || choice == "mixed" }
    var cloudRoles: [String] { choice == "mixed" ? ["drafting"] : engineRoles }
    var localRoles: [String] { choice == "mixed" ? ["main", "analysis"] : engineRoles }
    var selectedPreset: FirstRunPreset? { state?.presets.first { $0.id == provider } }

    func refresh() async {
        do {
            let data = try await send("api/first_run", method: "GET", body: nil)
            state = try JSONDecoder().decode(FirstRunState.self, from: data)
            if serverBase.isEmpty { serverBase = state?.local.serverBase ?? "" }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func startDownload() async {
        do {
            _ = try await send("api/first_run/download", method: "POST", body: [:])
            while true {
                await refresh()
                guard let d = state?.download, d.running == true else { break }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            if let err = state?.download.error { error = "Download failed: \(err)" }
        } catch {
            self.error = error.localizedDescription
        }
    }

    func listModels() async {
        guard !provider.isEmpty else { return }
        do {
            let data = try await send("api/providers/\(provider)/models", method: "POST",
                                      body: ["api_key": apiKey])
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            modelChoices = obj?["models"] as? [String] ?? []
            message = modelChoices.isEmpty ? "The provider listed no models." : "\(modelChoices.count) models listed."
        } catch {
            self.error = "List failed: \(error.localizedDescription)"
        }
    }

    func chooseFolder(_ role: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "Choose the MLX model folder for the \(engineRoleTitles[role] ?? role) engine"
        if panel.runModal() == .OK, let url = panel.url {
            for r in localRoles where (folders[r] ?? "").isEmpty || r == role {
                folders[r] = url.path
            }
        }
    }

    /// Save the choice, then restart the daemon so it builds those engines.
    func submit() async {
        guard let choice else { return }
        error = nil
        var body: [String: Any] = ["choice": choice]
        if needsLocal {
            body["local_source"] = localSource
            body["model_folders"] = folders
            body["server_base"] = serverBase
            body["server_models"] = serverModels
        }
        if needsCloud {
            body["provider"] = provider
            body["api_key"] = apiKey
            body["models"] = models.filter { !$0.value.isEmpty }
        }
        do {
            _ = try await send("api/first_run/choice", method: "POST", body: body)
            apiKey = ""
            message = "Saved. Restarting the daemon on your choice…"
            _ = try? await send("api/restart", method: "POST", body: [:])
            for _ in 0..<90 {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                if let data = try? await send("api/first_run", method: "GET", body: nil),
                   let s = try? JSONDecoder().decode(FirstRunState.self, from: data), !s.pending {
                    message = "Done. Next, the permissions bot-inator can use:"
                    done = true
                    return
                }
            }
            error = "The daemon did not come back after 90 s. Check its terminal."
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func send(_ path: String, method: String, body: [String: Any]?) async throws -> Data {
        var request = try DaemonAuth.request(baseURL.appendingPathComponent(path), method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        busy = true
        defer { busy = false }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw NSError(domain: "FirstRun", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"])
        }
        return data
    }
}

struct FirstRunView: View {
    @ObservedObject var model: FirstRunViewModel
    let close: () -> Void

    var body: some View {
        Form {
            if let state = model.state {
                header(state)
                Section("Choose an engine") {
                    ForEach(state.choices) { choiceRow($0, state: state) }
                }
                if model.needsLocal { localSection(state) }
                if model.needsCloud { cloudSection(state) }
                if model.done {
                    PermissionsView()
                }
                footer(state)
            } else {
                Text(model.error ?? "Waiting for the daemon…").foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(minWidth: 520, minHeight: 560)
        .task { await model.refresh() }
    }

    private func header(_ state: FirstRunState) -> some View {
        Section {
            Text("Welcome to bot-inator").appFont(.body).bold()
            if let gb = state.memoryGb {
                Text("This Mac has \(Int(gb.rounded())) GB of memory.").appFont(.secondary)
            }
            Text(state.recommended.reason).appFont(.secondary).foregroundStyle(.secondary)
        }
    }

    private func choiceRow(_ c: FirstRunChoice, state: FirstRunState) -> some View {
        Button {
            model.choice = c.id
        } label: {
            HStack(alignment: .top) {
                Image(systemName: model.choice == c.id ? "largecircle.fill.circle" : "circle")
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(c.title).bold()
                        if state.recommended.choice == c.id {
                            Text("Recommended for this Mac")
                                .appFont(.fine)
                                .padding(.horizontal, 6).padding(.vertical, 1)
                                .background(Capsule().fill(Color.accentColor.opacity(0.2)))
                        }
                    }
                    Text("Leaves this Mac: \(c.leavesMac)")
                        .appFont(.secondary).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(model.done)
    }

    @ViewBuilder
    private func localSection(_ state: FirstRunState) -> some View {
        Section(model.choice == "mixed" ? "Local models (main and analysis)" : "Local models") {
            Picker("Models from", selection: $model.localSource) {
                Text("Download the recommended set").tag("download")
                Text("A folder of MLX models").tag("folder")
                Text("Local server (Ollama, LM Studio)").tag("server")
            }
            switch model.localSource {
            case "folder":
                ForEach(model.localRoles, id: \.self) { role in
                    HStack {
                        Text(engineRoleTitles[role] ?? role)
                        Text(model.folders[role] ?? "not chosen")
                            .appFont(.secondary).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Choose…") { model.chooseFolder(role) }
                    }
                }
                Text("Each folder needs config.json and .safetensors weights. GGUF models need the Local server option.")
                    .appFont(.secondary).foregroundStyle(.secondary)
            case "server":
                TextField("Server URL", text: $model.serverBase)
                ForEach(model.localRoles, id: \.self) { role in
                    TextField("\(engineRoleTitles[role] ?? role) model", text: Binding(
                        get: { model.serverModels[role] ?? "" },
                        set: { model.serverModels[role] = $0 }))
                }
                Text("Prompts go to that server on this Mac. Keep it running.")
                    .appFont(.secondary).foregroundStyle(.secondary)
            default:
                downloadRow(state.download.total > 0 ? state.download : state.local.download)
            }
        }
    }

    private func downloadRow(_ d: FirstRunDownload) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            let gb = Double(d.total) / 1_000_000_000
            if d.total > 0 && d.done >= d.total {
                Label("Downloaded", systemImage: "checkmark.circle")
            } else {
                Button(String(format: "Download the models (%.1f GB)", gb)) {
                    Task { await model.startDownload() }
                }
                .disabled(d.running == true)
                if d.running == true, d.total > 0 {
                    ProgressView(value: Double(d.done), total: Double(d.total))
                }
            }
            Text("Nothing downloads until you click the button.")
                .appFont(.secondary).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func cloudSection(_ state: FirstRunState) -> some View {
        Section(model.choice == "mixed" ? "Cloud key (tool drafts only)" : "Cloud key") {
            Picker("Provider", selection: $model.provider) {
                Text("Choose…").tag("")
                ForEach(state.presets) { p in
                    Text(p.tested ? p.label : "\(p.label) (untested)").tag(p.id)
                }
            }
            if let preset = model.selectedPreset {
                if preset.keyRequired {
                    SecureField("Your \(preset.label) API key", text: $model.apiKey)
                }
                if !preset.note.isEmpty {
                    Text(preset.note).appFont(.secondary).foregroundStyle(.secondary)
                }
                ForEach(model.cloudRoles, id: \.self) { role in
                    HStack {
                        TextField("\(engineRoleTitles[role] ?? role) model", text: Binding(
                            get: { model.models[role] ?? "" },
                            set: { model.models[role] = $0 }),
                            prompt: Text(preset.defaultModels[role] ?? "pick one"))
                        if !model.modelChoices.isEmpty {
                            Menu("Choose") {
                                ForEach(model.modelChoices, id: \.self) { id in
                                    Button(id) { model.models[role] = id }
                                }
                            }
                            .fixedSize()
                        }
                    }
                }
                HStack {
                    Button("List models") { Task { await model.listModels() } }
                    if let url = URL(string: preset.docsUrl), !preset.docsUrl.isEmpty {
                        Spacer()
                        Link("Get a key", destination: url).appFont(.secondary)
                    }
                }
                Text("Prompts for these engines are sent to \(URL(string: preset.apiBase)?.host ?? preset.apiBase).")
                    .appFont(.secondary).foregroundStyle(.secondary)
            }
        }
    }

    private func footer(_ state: FirstRunState) -> some View {
        Section {
            if model.choice == "cloud" || model.choice == "mixed" {
                Text(state.ledgerLine).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let message = model.message { Text(message).appFont(.secondary) }
            if let error = model.error {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                if model.done {
                    Button("Finish") { close() }.keyboardShortcut(.defaultAction)
                } else {
                    Button("Use this engine") { Task { await model.submit() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(model.choice == nil || model.busy)
                }
            }
        }
    }
}

/// Opens the first-run window when the daemon says first run is pending.
/// Owned by ``AppDelegate``; asks once per launch, retrying while the
/// daemon starts.
@MainActor
final class FirstRunWindowController {
    static let shared = FirstRunWindowController()
    private var window: NSWindow?

    func showIfPending() {
        Task { @MainActor in
            let model = FirstRunViewModel()
            for _ in 0..<60 {
                await model.refresh()
                if let state = model.state {
                    if state.pending { present(model) }
                    return
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func present(_ model: FirstRunViewModel) {
        let view = FirstRunView(model: model) { [weak self] in self?.window?.close() }
            .appTextSize()
        let window = NSWindow(contentViewController: NSHostingController(rootView: view))
        window.title = "Set up bot-inator"
        window.styleMask.insert([.titled, .closable, .resizable])
        window.setContentSize(NSSize(width: 560, height: 640))
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
}
