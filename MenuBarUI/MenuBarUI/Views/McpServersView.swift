import SwiftUI

/// One tool a connected MCP server offers (#69). Tools are off until the
/// user switches them on; every call asks for approval unless the user
/// marked the tool "runs without asking". The server's read-only hint is
/// shown but never skips the approval card.
struct McpTool: Codable, Hashable, Identifiable {
    let name: String
    let prefixedName: String
    let description: String
    let enabled: Bool
    let runsWithoutAsking: Bool
    let readOnlyHint: Bool
    let blocked: String?
    let loaded: Bool

    var id: String { prefixedName }

    enum CodingKeys: String, CodingKey {
        case name, description, enabled, blocked, loaded
        case prefixedName = "prefixed_name"
        case runsWithoutAsking = "runs_without_asking"
        case readOnlyHint = "read_only_hint"
    }
}

/// One row of ``GET /api/mcp/servers``. Env values are write-only: the
/// daemon returns their names only.
struct McpServer: Codable, Hashable, Identifiable {
    let name: String
    let command: String
    let args: [String]
    let envKeys: [String]
    let status: String
    let error: String?
    let tools: [McpTool]

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, command, args, status, error, tools
        case envKeys = "env_keys"
    }
}

struct McpServersResponse: Codable {
    let servers: [McpServer]
    let maxLoaded: Int
    let enabledCount: Int

    enum CodingKeys: String, CodingKey {
        case servers
        case maxLoaded = "max_loaded"
        case enabledCount = "enabled_count"
    }
}

@MainActor
final class McpServersViewModel: ObservableObject {
    @Published private(set) var servers: [McpServer] = []
    @Published private(set) var maxLoaded: Int = 15
    @Published private(set) var enabledCount: Int = 0
    @Published private(set) var isLoading: Bool = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession
    private var pollTask: Task<Void, Never>?
    private var debounceTask: Task<Void, Never>?

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private var serversURL: URL { baseURL.appendingPathComponent("api/mcp/servers") }

    func refreshSoon(delay: TimeInterval = 0.3) {
        debounceTask?.cancel()
        debounceTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let request = try DaemonAuth.request(serversURL)
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response, data)
            let body = try JSONDecoder().decode(McpServersResponse.self, from: data)
            servers = body.servers
            maxLoaded = body.maxLoaded
            enabledCount = body.enabledCount
            errorMessage = nil
            pollWhileStarting()
        } catch {
            errorMessage = "Failed to fetch MCP servers: \(error.localizedDescription)"
        }
    }

    /// A server takes a few seconds to start; poll until none is starting.
    private func pollWhileStarting() {
        guard servers.contains(where: { $0.status == "starting" }) else { return }
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    func addServer(name: String, command: String, args: [String], env: [String: String]) async -> Bool {
        let body: [String: Any] = ["name": name, "command": command, "args": args, "env": env]
        return await send("POST", path: "api/mcp/servers", json: body,
                          failure: "Failed to add \(name)")
    }

    func remove(_ server: McpServer) async {
        _ = await send("DELETE", path: "api/mcp/servers/\(server.name)",
                       failure: "Failed to remove \(server.name)")
    }

    func restart(_ server: McpServer) async {
        _ = await send("POST", path: "api/mcp/servers/\(server.name)/restart",
                       failure: "Failed to restart \(server.name)")
    }

    func setEnabled(_ server: McpServer, _ tool: McpTool, _ enabled: Bool) async {
        _ = await send("PATCH", path: "api/mcp/servers/\(server.name)/tools/\(tool.name)",
                       json: ["enabled": enabled],
                       failure: "Failed to switch \(tool.prefixedName) \(enabled ? "on" : "off")")
    }

    func setRunsWithoutAsking(_ server: McpServer, _ tool: McpTool, _ free: Bool) async {
        _ = await send("PATCH", path: "api/mcp/servers/\(server.name)/tools/\(tool.name)",
                       json: ["runs_without_asking": free],
                       failure: "Failed to update \(tool.prefixedName)")
    }

    private func send(_ method: String, path: String, json: [String: Any]? = nil,
                      failure: String) async -> Bool {
        do {
            var request = try DaemonAuth.request(baseURL.appendingPathComponent(path), method: method)
            if let json {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONSerialization.data(withJSONObject: json)
            }
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response, data)
            errorMessage = nil
            await refresh()
            return true
        } catch {
            errorMessage = "\(failure): \(error.localizedDescription)"
            await refresh()
            return false
        }
    }

    /// Surfaces the daemon's ``detail`` (e.g. "at most 15 MCP tools…").
    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        try DaemonAuth.checkAuth(response)
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            var message = "HTTP \(http.statusCode)"
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let detail = obj["detail"] as? String {
                message = detail
            }
            throw NSError(domain: "McpServers", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: message])
        }
    }
}

/// Self-contained "MCP servers" section: embed with `McpServersView()`
/// (e.g. in Tool Manager under the tool list).
struct McpServersView: View {
    @StateObject private var viewModel = McpServersViewModel()
    @State private var showAdd = false
    @State private var pendingRemove: McpServer?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if viewModel.servers.isEmpty {
                Text("No MCP servers. Add a local (stdio) server to use another app's tools. New tools start switched off, and each call asks for your approval.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            } else {
                List {
                    ForEach(viewModel.servers) { server in
                        Section {
                            ForEach(server.tools) { tool in
                                toolRow(server, tool)
                            }
                        } header: {
                            serverHeader(server)
                        }
                    }
                }
                .listStyle(.inset)
                .frame(minHeight: 140, maxHeight: 320)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
        .onReceive(NotificationCenter.default.publisher(for: .toolsChanged)) { _ in
            viewModel.refreshSoon()
        }
        .sheet(isPresented: $showAdd) {
            AddMcpServerSheet(viewModel: viewModel)
                .appTextSize()
        }
        .confirmationDialog(
            "Remove \(pendingRemove?.name ?? "")?",
            isPresented: .init(
                get: { pendingRemove != nil },
                set: { if !$0 { pendingRemove = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Remove", role: .destructive) {
                if let server = pendingRemove {
                    Task { await viewModel.remove(server) }
                }
                pendingRemove = nil
            }
            Button("Cancel", role: .cancel) { pendingRemove = nil }
        } message: {
            Text("Stops the server and deletes its settings, including any secrets stored for it.")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "puzzlepiece.extension")
                .foregroundStyle(.secondary)
            Text("MCP servers").appFont(.headline)
            Text("\(viewModel.enabledCount)/\(viewModel.maxLoaded) tools on")
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
            .help("Reload MCP servers")
            Button("Add…") { showAdd = true }
                .controlSize(.small)
        }
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running": return .green
        case "starting": return .yellow
        case "crashed", "failed": return .orange
        default: return .secondary
        }
    }

    private func serverHeader(_ server: McpServer) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Circle().fill(statusColor(server.status)).frame(width: 8, height: 8)
                Text(server.name).appFont(.body, design: .monospaced)
                Text(server.status).appFont(.fine).foregroundStyle(.secondary)
                Spacer()
                if server.status != "starting" {
                    Button {
                        Task { await viewModel.restart(server) }
                    } label: {
                        Image(systemName: "arrow.triangle.2.circlepath")
                    }
                    .buttonStyle(.borderless)
                    .help("Restart this server")
                }
                Button(role: .destructive) {
                    pendingRemove = server
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help("Remove this server")
            }
            Text(([server.command] + server.args).joined(separator: " "))
                .appFont(.fine, design: .monospaced)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
            if !server.envKeys.isEmpty {
                Text("Env: \(server.envKeys.joined(separator: ", "))")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
            }
            if let error = server.error, !error.isEmpty {
                Text(error)
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
                    .lineLimit(3)
            }
        }
    }

    private func toolRow(_ server: McpServer, _ tool: McpTool) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(tool.prefixedName)
                    .appFont(.body, design: .monospaced)
                    .foregroundStyle(tool.loaded ? .primary : .secondary)
                if !tool.description.isEmpty {
                    Text(tool.description)
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if let blocked = tool.blocked {
                    Text("Can't be used: \(blocked)")
                        .appFont(.secondary)
                        .foregroundStyle(.orange)
                } else if tool.readOnlyHint {
                    Text("The server says this tool only reads. It still asks first unless you choose otherwise.")
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
                if tool.enabled {
                    Toggle("Runs without asking", isOn: .init(
                        get: { tool.runsWithoutAsking },
                        set: { free in
                            Task { await viewModel.setRunsWithoutAsking(server, tool, free) }
                        }
                    ))
                    .toggleStyle(.checkbox)
                    .appFont(.secondary)
                    .help("Skip the approval card for this tool. Only do this for tools that can't change or send anything.")
                }
            }
            Spacer()
            Toggle("", isOn: .init(
                get: { tool.enabled },
                set: { enabled in
                    Task { await viewModel.setEnabled(server, tool, enabled) }
                }
            ))
            .toggleStyle(.switch)
            .controlSize(.small)
            .labelsHidden()
            .disabled(tool.blocked != nil)
            .help(tool.enabled
                  ? "Switch off: the agent stops seeing this tool"
                  : "Switch on: the agent can ask to use this tool")
        }
        .padding(.vertical, 2)
    }
}

/// Form for a new stdio server: name, command, args (one per line) and
/// env (`KEY=value`, one per line). Env values go to the daemon's 0600
/// config file and are never shown again.
private struct AddMcpServerSheet: View {
    @ObservedObject var viewModel: McpServersViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var command = ""
    @State private var argsText = ""
    @State private var envText = ""
    @State private var isSaving = false

    private var parsedArgs: [String] {
        argsText.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private var parsedEnv: [String: String] {
        var env: [String: String] = [:]
        for line in envText.split(whereSeparator: \.isNewline) {
            let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { continue }
            let key = parts[0].trimmingCharacters(in: .whitespaces)
            if !key.isEmpty { env[key] = parts[1] }
        }
        return env
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Add MCP server").appFont(.headline)
            Text("Local (stdio) servers only: the daemon starts the command as a child process. Its tools start switched off.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            TextField("Name (e.g. github)", text: $name)
                .textFieldStyle(.roundedBorder)
            TextField("Command (e.g. npx)", text: $command)
                .textFieldStyle(.roundedBorder)
            Text("Arguments, one per line").appFont(.fine).foregroundStyle(.secondary)
            TextEditor(text: $argsText)
                .appFont(.body, design: .monospaced)
                .frame(height: 60)
                .border(Color.secondary.opacity(0.3))
            Text("Environment, KEY=value per line (stored privately, never shown again)")
                .appFont(.fine).foregroundStyle(.secondary)
            TextEditor(text: $envText)
                .appFont(.body, design: .monospaced)
                .frame(height: 60)
                .border(Color.secondary.opacity(0.3))
            if let error = viewModel.errorMessage {
                Text(error).appFont(.secondary).foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Add") {
                    isSaving = true
                    Task {
                        let ok = await viewModel.addServer(
                            name: name.trimmingCharacters(in: .whitespaces),
                            command: command.trimmingCharacters(in: .whitespaces),
                            args: parsedArgs, env: parsedEnv)
                        isSaving = false
                        if ok { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(isSaving || name.isEmpty || command.isEmpty)
            }
        }
        .padding(16)
        .frame(width: 420)
    }
}
