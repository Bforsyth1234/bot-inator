import SwiftUI

/// One row of ``GET /api/ideas/declined`` (#56): a tool idea the user
/// declined, grouped by name. Declined ideas block reworded repeats; this
/// screen lets the user un-decline one, or all.
struct DeclinedIdea: Codable, Hashable, Identifiable {
    let name: String
    let description: String
    let lastDeclinedAt: Double
    let count: Int

    var id: String { name }
    var lastDeclinedDate: Date { Date(timeIntervalSince1970: lastDeclinedAt) }

    enum CodingKeys: String, CodingKey {
        case name, description, count
        case lastDeclinedAt = "last_declined_at"
    }
}

@MainActor
final class DeclinedIdeasViewModel: ObservableObject {
    @Published private(set) var ideas: [DeclinedIdea] = []
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
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/ideas/declined"))
            let (data, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            ideas = try JSONDecoder().decode([DeclinedIdea].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to fetch declined ideas: \(error.localizedDescription)"
        }
    }

    func remove(_ idea: DeclinedIdea) async {
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/ideas/declined/\(idea.name)"),
                method: "DELETE")
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to remove \(idea.name): \(error.localizedDescription)"
        }
    }

    func clearAll() async {
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/ideas/declined/clear"),
                method: "POST")
            let (_, response) = try await session.data(for: request)
            try Self.checkStatus(response)
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to clear declined ideas: \(error.localizedDescription)"
        }
    }

    private static func checkStatus(_ response: URLResponse) throws {
        try DaemonAuth.checkAuth(response)
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            throw NSError(
                domain: "DeclinedIdeas", code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
    }
}

/// Self-contained "Declined ideas" section: embed with `DeclinedIdeasView()`.
struct DeclinedIdeasView: View {
    @StateObject private var viewModel = DeclinedIdeasViewModel()
    @State private var confirmClear = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if viewModel.ideas.isEmpty {
                Text("No declined ideas. Ideas you deny are remembered here so they aren't asked again.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                List {
                    ForEach(viewModel.ideas) { idea in
                        row(for: idea)
                    }
                }
                .listStyle(.inset)
                .frame(minHeight: 120, maxHeight: 220)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
        .confirmationDialog(
            "Forget all \(viewModel.ideas.count) declined ideas?",
            isPresented: $confirmClear,
            titleVisibility: .visible
        ) {
            Button("Clear all", role: .destructive) {
                Task { await viewModel.clearAll() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The agent may suggest these tools again. Approved ideas are kept.")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "hand.raised.circle")
                .foregroundStyle(.secondary)
            Text("Declined ideas").font(.headline)
            Text("\(viewModel.ideas.count)")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(viewModel.isLoading)
            .help("Reload declined ideas")
            Button("Clear all", role: .destructive) { confirmClear = true }
                .controlSize(.small)
                .disabled(viewModel.ideas.isEmpty)
        }
    }

    private func row(for idea: DeclinedIdea) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(idea.name)
                    .font(.system(.body, design: .monospaced))
                if !idea.description.isEmpty {
                    Text(idea.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Text("Declined \(idea.count)× · last \(idea.lastDeclinedDate.formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Remove") {
                Task { await viewModel.remove(idea) }
            }
            .controlSize(.small)
            .help("Un-decline: the agent may suggest this tool again")
        }
        .padding(.vertical, 2)
    }
}
