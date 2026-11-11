import SwiftUI

/// One row of `GET /api/actions` (#83): something the agent did.
struct ActionEntry: Codable, Hashable, Identifiable {
    let id: String
    let createdAt: Double
    let tool: String
    let label: String
    let why: String
    let source: String
    let undoable: Bool
    let canUndo: Bool
    let notUndoableReason: String
    let undoneAt: Double?
    let undoStatus: String?
    let undoMessage: String?

    enum CodingKeys: String, CodingKey {
        case id, tool, label, why, source, undoable
        case createdAt = "created_at"
        case canUndo = "can_undo"
        case notUndoableReason = "not_undoable_reason"
        case undoneAt = "undone_at"
        case undoStatus = "undo_status"
        case undoMessage = "undo_message"
    }
}

/// `POST /api/actions/{id}/undo` answer: `undone`, or why not.
struct UndoResult: Codable, Hashable {
    let status: String
    let message: String
    let entry: ActionEntry?
}

@MainActor
final class ActionHistoryViewModel: ObservableObject {
    @Published private(set) var entries: [ActionEntry] = []
    @Published private(set) var loaded = false
    @Published private(set) var busy: Set<String> = []
    @Published var errorMessage: String?
    /// The last undo's answer per entry, shown under that row.
    @Published private(set) var messages: [String: String] = [:]

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func refresh() async {
        do {
            let request = try DaemonAuth.request(baseURL.appendingPathComponent("api/actions"))
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, allowed: 200..<300)
            entries = try JSONDecoder().decode([ActionEntry].self, from: data)
            loaded = true
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// The user's click: no approval card (it is not the agent acting).
    func undo(_ entry: ActionEntry) async {
        busy.insert(entry.id)
        defer { busy.remove(entry.id) }
        do {
            let url = baseURL.appendingPathComponent("api/actions/\(entry.id)/undo")
            let request = try DaemonAuth.request(url, method: "POST")
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            // 409 carries the refusal ("changed since, not undone").
            try Self.checkStatus(response, allowed: 200..<500)
            let result = try JSONDecoder().decode(UndoResult.self, from: data)
            messages[entry.id] = result.message
            if let updated = result.entry, let index = entries.firstIndex(where: { $0.id == updated.id }) {
                entries[index] = updated
            }
            errorMessage = nil
        } catch {
            errorMessage = "Undo failed: " + error.localizedDescription
        }
    }

    private static func checkStatus(_ response: URLResponse, allowed: Range<Int>) throws {
        if let http = response as? HTTPURLResponse, !allowed.contains(http.statusCode) {
            throw NSError(domain: "ActionHistory", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
    }
}

/// "History" (#83): what the agent did, when and why, with Undo for the
/// actions the journal can take back. Entries are kept 30 days.
struct ActionHistoryView: View {
    @StateObject private var viewModel = ActionHistoryViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("What the assistant did")
                    .appFont(.secondary, weight: .semibold)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Refresh") { Task { await viewModel.refresh() } }
                    .buttonStyle(.borderless)
                    .appFont(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if viewModel.loaded && viewModel.entries.isEmpty {
                Text("Nothing yet. Approved actions show up here for 30 days.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            ForEach(viewModel.entries) { entry in
                row(entry)
                Divider()
            }
        }
        .task { await viewModel.refresh() }
    }

    private func row(_ entry: ActionEntry) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(entry.label.isEmpty ? entry.tool : entry.label)
                        .appFont(.body)
                        .lineLimit(2)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                    Text(Self.detail(entry))
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                trailing(entry)
            }
            if let message = viewModel.messages[entry.id] ?? Self.lastRefusal(entry) {
                Text(message)
                    .appFont(.fine)
                    .foregroundStyle(entry.undoneAt != nil ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func trailing(_ entry: ActionEntry) -> some View {
        if entry.undoneAt != nil {
            Label("Undone", systemImage: "arrow.uturn.backward")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else if entry.canUndo {
            Button("Undo") { Task { await viewModel.undo(entry) } }
                .appFont(.secondary)
                .disabled(viewModel.busy.contains(entry.id))
        } else {
            Text("Can't undo")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .help(entry.notUndoableReason.isEmpty ? (entry.undoMessage ?? "") : entry.notUndoableReason)
        }
    }

    /// The stored answer of the last refused undo, after a refresh.
    private static func lastRefusal(_ entry: ActionEntry) -> String? {
        guard entry.undoneAt == nil, let status = entry.undoStatus, status != "undone" else { return nil }
        return entry.undoMessage
    }

    private static let timeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    static func detail(_ entry: ActionEntry) -> String {
        let when = timeFormatter.localizedString(
            for: Date(timeIntervalSince1970: entry.createdAt), relativeTo: Date())
        var parts = [entry.tool, when]
        if !entry.why.isEmpty { parts.append("after \(entry.why.replacingOccurrences(of: "_", with: " "))") }
        if entry.source == "rule" { parts.append("by a rule") }
        return parts.joined(separator: " · ")
    }
}
