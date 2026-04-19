import SwiftUI

/// Plain-value row returned by ``GET /api/tools``. Built-in tools are
/// shipped with the daemon; generated ones live under
/// ``agent-daemon/tools/generated/`` and can be removed from this UI.
struct AgentTool: Codable, Hashable, Identifiable {
    let name: String
    let description: String
    let isGenerated: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, description
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

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let (data, response) = try await session.data(
                from: baseURL.appendingPathComponent("api/tools")
            )
            try Self.checkStatus(response)
            tools = try JSONDecoder().decode([AgentTool].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to fetch tools: \(error.localizedDescription)"
        }
    }

    func delete(_ tool: AgentTool) async {
        guard tool.isGenerated else { return }
        var request = URLRequest(
            url: baseURL.appendingPathComponent("api/tools/\(tool.name)")
        )
        request.httpMethod = "DELETE"
        do {
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to delete \(tool.name): \(error.localizedDescription)"
        }
    }

    private static func checkStatus(_ response: URLResponse) throws {
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

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .frame(minWidth: 440, minHeight: 420)
        .task { await viewModel.refresh() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "hammer.circle.fill")
                .foregroundStyle(Color.accentColor)
                .font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text("Manage Tools").font(.headline)
                Text("\(viewModel.tools.count) registered")
                    .font(.caption)
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
            Text("This removes the generated module from disk and unloads it from the running agent.")
        }
    }

    private func row(for tool: AgentTool) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: tool.isGenerated ? "sparkles" : "hammer")
                .foregroundStyle(tool.isGenerated ? .purple : .secondary)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(tool.name)
                    .font(.system(.body, design: .monospaced))
                if !tool.description.isEmpty {
                    Text(tool.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if tool.isGenerated {
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
