import SwiftUI

/// Training data (#118): the opt-in corpus of the prompt and card behind
/// each decision. Off by default; email and message text need a second,
/// separate toggle. Kept only on this Mac (~/.bot-inator/training.db).
/// Turning it off offers "Delete what's stored" (default yes).
///
/// Routes: `GET /api/training`, `PUT /api/training/settings`,
/// `GET /api/training/rows?limit&offset`, `DELETE /api/training/rows/{id}`,
/// `DELETE /api/training/rows`.

struct TrainingSummary: Codable, Equatable {
    let enabled: Bool
    let keepMailText: Bool
    let lockedFields: [String]
    let count: Int
    let usable: Int
    let negatives: Int
    let tainted: Int
    let heldout: Int

    enum CodingKeys: String, CodingKey {
        case enabled, count, usable, negatives, tainted, heldout
        case keepMailText = "keep_mail_text"
        case lockedFields = "locked_fields"
    }
}

struct TrainingCard: Codable, Hashable {
    let tool: String?
    let summary: String?
    let reasoning: String?
}

struct TrainingRowEntry: Codable, Hashable, Identifiable {
    let id: String
    let createdAt: Double
    let toolName: String
    let trigger: String
    let verdict: String
    let outcome: String?
    let tainted: Bool
    let mailStripped: Bool
    let heldout: Bool?
    let prompt: String
    let card: TrainingCard?

    enum CodingKeys: String, CodingKey {
        case id, trigger, verdict, outcome, tainted, heldout, prompt, card
        case createdAt = "created_at"
        case toolName = "tool_name"
        case mailStripped = "mail_stripped"
    }
}

private struct TrainingRowsPage: Codable {
    let rows: [TrainingRowEntry]
    let total: Int
    let limit: Int
    let offset: Int
}

private struct TrainingSettingsUpdate: Encodable {
    var enabled: Bool?
    var keepMailText: Bool?
    var deleteStored: Bool?

    enum CodingKeys: String, CodingKey {
        case enabled
        case keepMailText = "keep_mail_text"
        case deleteStored = "delete_stored"
    }
}

@MainActor
final class TrainingDataViewModel: ObservableObject {
    static let pageSize = 50

    @Published private(set) var summary: TrainingSummary?
    @Published private(set) var rows: [TrainingRowEntry] = []
    @Published private(set) var total = 0
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    var hasMore: Bool { rows.count < total }

    func isLocked(_ field: String) -> Bool { summary?.lockedFields.contains(field) ?? false }

    private func send(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        var request = try DaemonAuth.request(url, method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = (json?["detail"] as? String) ?? (json?["message"] as? String)
            throw NSError(domain: "TrainingData", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        do {
            let data = try await send("/api/training")
            summary = try JSONDecoder().decode(TrainingSummary.self, from: data)
            let page = try JSONDecoder().decode(
                TrainingRowsPage.self,
                from: try await send("/api/training/rows?limit=\(Self.pageSize)&offset=0")
            )
            rows = page.rows
            total = page.total
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load training data: \(error.localizedDescription)"
        }
    }

    func loadMore() async {
        do {
            let page = try JSONDecoder().decode(
                TrainingRowsPage.self,
                from: try await send("/api/training/rows?limit=\(Self.pageSize)&offset=\(rows.count)")
            )
            let known = Set(rows.map(\.id))
            rows.append(contentsOf: page.rows.filter { !known.contains($0.id) })
            total = page.total
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load more: \(error.localizedDescription)"
        }
    }

    func setEnabled(_ value: Bool, deleteStored: Bool = true) async {
        await update(TrainingSettingsUpdate(enabled: value, deleteStored: value ? nil : deleteStored),
                     status: value ? "Keeping training data on this Mac."
                                   : (deleteStored ? "Off. Stored training data deleted." : "Off. Stored data kept."))
    }

    func setKeepMailText(_ value: Bool) async {
        await update(TrainingSettingsUpdate(keepMailText: value),
                     status: value ? "Email and message text will be kept." : "Email and message text won't be kept.")
    }

    private func update(_ body: TrainingSettingsUpdate, status: String) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let data = try await send("/api/training/settings", method: "PUT",
                                      body: try JSONEncoder().encode(body))
            summary = try JSONDecoder().decode(TrainingSummary.self, from: data)
            statusMessage = status
            errorMessage = nil
        } catch {
            errorMessage = "Save failed: \(error.localizedDescription)"
        }
        await refresh()
    }

    func remove(_ row: TrainingRowEntry) async {
        do {
            _ = try await send("/api/training/rows/\(row.id)", method: "DELETE")
            rows.removeAll { $0.id == row.id }
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't delete it: \(error.localizedDescription)"
        }
        await refresh()
    }

    func removeAll() async {
        do {
            _ = try await send("/api/training/rows", method: "DELETE")
            statusMessage = "All training data deleted."
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't delete: \(error.localizedDescription)"
        }
        await refresh()
    }
}

/// Self-contained "Training data" section: embed with `TrainingDataView()`.
struct TrainingDataView: View {
    @StateObject private var viewModel = TrainingDataViewModel()
    @State private var confirmDeleteAll = false
    @State private var confirmTurnOff = false
    @State private var expanded: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Text("Keeps the prompt and the card behind each of your decisions, so a personal model can learn from them later. Stays on this Mac; never sent anywhere.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            toggles
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            } else if let status = viewModel.statusMessage {
                Text(status).appFont(.fine).foregroundStyle(.secondary)
            }
            if let summary = viewModel.summary {
                Text(countText(summary))
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            if viewModel.rows.isEmpty {
                Text("Nothing stored.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(viewModel.rows) { row in
                    rowView(row)
                    Divider()
                }
                HStack {
                    if viewModel.hasMore {
                        Button("Show more") { Task { await viewModel.loadMore() } }
                            .controlSize(.small)
                    }
                    Spacer()
                    Button("Delete all", role: .destructive) { confirmDeleteAll = true }
                        .controlSize(.small)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
        .confirmationDialog("Delete all training data?", isPresented: $confirmDeleteAll) {
            Button("Delete all", role: .destructive) { Task { await viewModel.removeAll() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Every stored decision is deleted, including the held-out set. This can't be undone.")
        }
        .confirmationDialog("Stop keeping training data?", isPresented: $confirmTurnOff) {
            Button("Turn off and delete what's stored", role: .destructive) {
                Task { await viewModel.setEnabled(false, deleteStored: true) }
            }
            Button("Turn off, keep what's stored") {
                Task { await viewModel.setEnabled(false, deleteStored: false) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Nothing new is kept once it's off.")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "brain")
                .foregroundStyle(.secondary)
            Text("Training data").appFont(.headline)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload")
        }
    }

    private var toggles: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle("Keep training data", isOn: Binding(
                get: { viewModel.summary?.enabled ?? false },
                set: { value in
                    if value {
                        Task { await viewModel.setEnabled(true) }
                    } else {
                        confirmTurnOff = true
                    }
                }
            ))
            .disabled(viewModel.isBusy || viewModel.summary == nil || viewModel.isLocked("enabled"))
            Toggle("Include email and message text", isOn: Binding(
                get: { viewModel.summary?.keepMailText ?? false },
                set: { value in Task { await viewModel.setKeepMailText(value) } }
            ))
            .disabled(viewModel.isBusy || !(viewModel.summary?.enabled ?? false)
                      || viewModel.isLocked("keep_mail_text"))
            .padding(.leading, 16)
            Text("Without it, the text of emails and messages is left out of each row.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
                .padding(.leading, 16)
        }
        .appFont(.body)
    }

    private func countText(_ summary: TrainingSummary) -> String {
        var parts = [summary.count == 1 ? "1 decision stored" : "\(summary.count) decisions stored"]
        if summary.tainted > 0 {
            parts.append("\(summary.tainted) left out of training (outside text)")
        }
        if summary.heldout > 0 {
            parts.append("\(summary.heldout) held out for testing")
        }
        return parts.joined(separator: " · ")
    }

    private func rowView(_ row: TrainingRowEntry) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title(row)).appFont(.body).lineLimit(2)
                Text(detail(row))
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                if expanded.contains(row.id) {
                    Text(row.prompt.isEmpty ? "(no prompt)" : row.prompt)
                        .appFont(.fine)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 2)
                }
                Button(expanded.contains(row.id) ? "Hide prompt" : "Show prompt") {
                    if expanded.contains(row.id) {
                        expanded.remove(row.id)
                    } else {
                        expanded.insert(row.id)
                    }
                }
                .buttonStyle(.link)
                .appFont(.fine)
            }
            Spacer()
            Button("Delete", role: .destructive) {
                Task { await viewModel.remove(row) }
            }
            .controlSize(.small)
        }
        .padding(.vertical, 2)
    }

    private func title(_ row: TrainingRowEntry) -> String {
        if let summary = row.card?.summary, !summary.isEmpty { return summary }
        return row.toolName
    }

    private func detail(_ row: TrainingRowEntry) -> String {
        let date = Date(timeIntervalSince1970: row.createdAt)
        var parts = [date.formatted(date: .abbreviated, time: .shortened), row.toolName, verdictText(row)]
        if row.tainted { parts.append("outside text, not used") }
        if row.mailStripped { parts.append("email text left out") }
        if row.heldout == true { parts.append("held out") }
        return parts.joined(separator: " · ")
    }

    private func verdictText(_ row: TrainingRowEntry) -> String {
        var text: String
        switch row.verdict {
        case "approved": text = "Approved"
        case "edited": text = "Edited, then approved"
        case "denied": text = "Denied"
        case "timed_out": text = "No answer"
        default: text = row.verdict
        }
        switch row.outcome {
        case "undone": text += ", undone"
        case "tool_never_run": text += ", tool never used"
        default: break
        }
        return text
    }
}
