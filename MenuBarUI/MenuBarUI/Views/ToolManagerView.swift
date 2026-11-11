import SwiftUI

/// Plain-value row returned by ``GET /api/tools``. Built-in tools are
/// shipped with the daemon; generated ones live under
/// ``agent-daemon/tools/generated/`` and can be removed, disabled
/// (moved to ``generated/disabled/``) or re-enabled from this UI. A tool
/// that failed to load sits in ``generated/quarantine/`` with its error.
struct AgentTool: Codable, Hashable, Identifiable {
    let name: String
    let description: String
    let isGenerated: Bool
    let disabled: Bool?
    let quarantined: Bool?
    let error: String?

    var id: String { name }
    var isDisabled: Bool { disabled ?? false }
    var isQuarantined: Bool { quarantined ?? false }
    var isEnabled: Bool { !isDisabled && !isQuarantined }

    enum CodingKeys: String, CodingKey {
        case name, description, disabled, quarantined, error
        case isGenerated = "is_generated"
    }
}

@MainActor
final class ToolManagerViewModel: ObservableObject {
    @Published private(set) var tools: [AgentTool] = []
    @Published private(set) var isLoading: Bool = false
    @Published var errorMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private var debounceTask: Task<Void, Never>?

    /// Refresh shortly after a `tools_changed` broadcast (#29). A burst of
    /// frames (disable, then the window's own refresh) collapses into one
    /// fetch.
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
            let request = try DaemonAuth.request(baseURL.appendingPathComponent("api/tools"))
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            tools = try JSONDecoder().decode([AgentTool].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to fetch tools: \(error.localizedDescription)"
        }
    }

    func delete(_ tool: AgentTool) async {
        guard tool.isGenerated else { return }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/tools/\(tool.name)"), method: "DELETE"
            )
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to delete \(tool.name): \(error.localizedDescription)"
        }
    }

    /// POST ``/api/tools/<name>/enable`` or ``/disable``. A tool that fails
    /// to load again comes back quarantined; its error shows in the row.
    func setEnabled(_ tool: AgentTool, _ enabled: Bool) async {
        guard tool.isGenerated else { return }
        let action = enabled ? "enable" : "disable"
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/tools/\(tool.name)/\(action)"),
                method: "POST"
            )
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to \(action) \(tool.name): \(error.localizedDescription)"
        }
    }

    private static func checkStatus(_ response: URLResponse) throws {
        try DaemonAuth.checkAuth(response)
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            throw NSError(
                domain: "ToolManager", code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey:
                    "HTTP \(http.statusCode)"]
            )
        }
    }
}

struct ToolManagerView: View {
    @StateObject private var viewModel = ToolManagerViewModel()
    @State private var pendingDelete: AgentTool?
    /// The row the user clicked; its detail (#61) shows in a sheet.
    @State private var selectedTool: AgentTool?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
            // DECLINED IDEAS (#56): review and remove remembered denials.
            Divider()
            DeclinedIdeasView()
            // LEARNED PREFERENCES (#72): what card decisions taught the agent.
            Divider()
            LearnedPreferencesView()
            // MCP SERVERS (#69): other apps' local tools, off until enabled.
            Divider()
            McpServersView()
        }
        .frame(minWidth: 440, minHeight: 420)
        .sheet(item: $selectedTool) { tool in
            ToolDetailView(toolName: tool.name)
                .appTextSize()
        }
        .task { await viewModel.refresh() }
        .onReceive(NotificationCenter.default.publisher(for: .toolsChanged)) { _ in
            viewModel.refreshSoon()
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "hammer.circle.fill")
                .foregroundStyle(Color.accentColor)
                .appFont(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text("Manage Tools").appFont(.headline)
                Text("\(viewModel.tools.count) registered")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(viewModel.isLoading)
        }
        .padding(14)
    }

    @ViewBuilder
    private var content: some View {
        if let error = viewModel.errorMessage {
            Label(error, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .padding(12)
        }
        List {
            ForEach(viewModel.tools) { tool in
                row(for: tool)
                    .contentShape(Rectangle())
                    .onTapGesture { selectedTool = tool }
                    .help("Show what \(tool.name) does, when it runs and its source")
            }
        }
        .listStyle(.inset)
        .confirmationDialog(
            "Delete \(pendingDelete?.name ?? "")?",
            isPresented: .init(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                if let tool = pendingDelete {
                    Task { await viewModel.delete(tool) }
                }
                pendingDelete = nil
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: {
            Text("This removes the generated module from disk (including a disabled or quarantined copy) and unloads it from the running agent.")
        }
    }

    private func row(for tool: AgentTool) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: tool.isQuarantined
                  ? "exclamationmark.triangle"
                  : (tool.isGenerated ? "sparkles" : "hammer"))
                .foregroundStyle(tool.isQuarantined
                                 ? .orange
                                 : (tool.isGenerated ? .purple : .secondary))
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(tool.name)
                    .appFont(.body, design: .monospaced)
                    .foregroundStyle(tool.isEnabled ? .primary : .secondary)
                if !tool.description.isEmpty {
                    Text(tool.description)
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if tool.isQuarantined {
                    Text("Failed to load: \(tool.error ?? "unknown error")")
                        .appFont(.secondary)
                        .foregroundStyle(.orange)
                        .lineLimit(2)
                } else if tool.isDisabled {
                    Text("Disabled")
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button {
                selectedTool = tool
            } label: {
                Image(systemName: "info.circle")
            }
            .buttonStyle(.borderless)
            .help("Details and source")
            if tool.isGenerated {
                Toggle("", isOn: .init(
                    get: { tool.isEnabled },
                    set: { enabled in
                        Task { await viewModel.setEnabled(tool, enabled) }
                    }
                ))
                .toggleStyle(.switch)
                .controlSize(.small)
                .labelsHidden()
                .help(tool.isEnabled
                      ? "Disable this tool without deleting it"
                      : "Enable this tool and load it again")
                Button(role: .destructive) {
                    pendingDelete = tool
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help("Uninstall this AI-generated tool")
            }
        }
        .padding(.vertical, 2)
    }
}
