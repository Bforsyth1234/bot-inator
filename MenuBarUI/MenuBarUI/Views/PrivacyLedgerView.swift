import SwiftUI

/// One row of `GET /api/egress` (#82): a request that left this Mac.
struct EgressRow: Codable, Identifiable, Equatable {
    var id: Int
    var time: String
    var kind: String
    var host: String?
    var role: String?
    var tool: String?
    var model: String?
    var bytesOut: Int
    var bytesIn: Int
    var personalText: String?
    var note: String?
    var text: String?

    enum CodingKeys: String, CodingKey {
        case id, time, kind, host, role, tool, model, note, text
        case bytesOut = "bytes_out"
        case bytesIn = "bytes_in"
        case personalText = "personal_text"
    }

    /// "Groq · main" / "MCP tool github__search" / "Browser".
    var what: String {
        switch kind {
        case "inference":
            return [model, role.map { "\($0) engine" }].compactMap { $0 }.joined(separator: " · ")
        case "mcp_tool": return "MCP tool \(tool ?? "")"
        case "sandboxed_tool": return "Tool \(tool ?? "")"
        // #109: a generated tool the user trusted, run inside bot-inator.
        case "trusted_tool": return "Trusted tool \(tool ?? "")"
        // #110: a screenshot sent to the user's own phone.
        case "imessage_image": return "Screenshot sent to your phone (iMessage)"
        case "open_url": return "Opened in browser"
        case "model_download": return "Model download \(model ?? "")"
        case "imessage": return "iMessage to you"
        default: return kind
        }
    }

    var destination: String {
        // #109: no host on a trusted-tool row = a program it launched.
        host ?? (kind == "trusted_tool" ? "a program it launched" : "its own network access")
    }

    var shortTime: String {
        guard let date = ISO8601DateFormatter.egress.date(from: time) else { return time }
        return DateFormatter.egressShort.string(from: date)
    }
}

/// `today` in `GET /api/egress`, and the body of `GET /api/egress/stats`.
struct EgressStats: Codable, Equatable {
    var day: String
    var count: Int
    var bytesOut: Int
    var bytesIn: Int
    var summary: String

    enum CodingKeys: String, CodingKey {
        case day, count, summary
        case bytesOut = "bytes_out"
        case bytesIn = "bytes_in"
    }
}

private struct EgressListing: Codable {
    var rows: [EgressRow]
    var today: EgressStats
    var keepText: Bool
    var lockedFields: [String]
    var limitNote: String

    enum CodingKeys: String, CodingKey {
        case rows, today
        case keepText = "keep_text"
        case lockedFields = "locked_fields"
        case limitNote = "limit_note"
    }
}

private struct EgressUpdate: Encodable {
    var keepText: Bool
    enum CodingKeys: String, CodingKey { case keepText = "keep_text" }
}

extension ISO8601DateFormatter {
    fileprivate static let egress: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
}

extension DateFormatter {
    fileprivate static let egressShort: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .short
        return f
    }()
}

private func egressBytes(_ n: Int) -> String {
    ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)
}

@MainActor
final class PrivacyLedgerViewModel: ObservableObject {
    @Published private(set) var rows: [EgressRow] = []
    @Published private(set) var today: EgressStats?
    @Published private(set) var keepText = false
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var limitNote = ""
    @Published private(set) var loaded = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func isLocked(_ field: String) -> Bool { lockedFields.contains(field) }

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    func setKeepText(_ value: Bool) async {
        keepText = value
        await send(method: "PUT", body: EgressUpdate(keepText: value),
                   status: value ? "Request text is kept for 7 days."
                                 : "Request text is not kept; kept text was erased.")
    }

    func clear() async {
        await send(method: "DELETE", body: nil, status: "Privacy ledger cleared.")
    }

    private func send(method: String, body: EgressUpdate?, status: String?) async {
        do {
            var url = baseURL.appendingPathComponent("api/egress")
            if method == "GET" {
                var parts = URLComponents(url: url, resolvingAgainstBaseURL: false)
                parts?.queryItems = [URLQueryItem(name: "limit", value: "200")]
                url = parts?.url ?? url
            }
            var request = try DaemonAuth.request(url, method: method)
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw NSError(domain: "PrivacyLedger", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
            }
            let listing = try JSONDecoder().decode(EgressListing.self, from: data)
            rows = listing.rows
            today = listing.today
            keepText = listing.keepText
            lockedFields = listing.lockedFields
            limitNote = listing.limitNote
            loaded = true
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (method == "GET" ? "" : "Save failed: ") + error.localizedDescription
        }
    }
}

/// "Privacy ledger" section for Settings (#82): everything that left this Mac.
struct PrivacyLedgerView: View {
    @StateObject private var viewModel = PrivacyLedgerViewModel()
    @State private var confirmClear = false
    @State private var showAll = false

    private var visibleRows: [EgressRow] {
        showAll ? viewModel.rows : Array(viewModel.rows.prefix(10))
    }

    var body: some View {
        Section {
            if let today = viewModel.today {
                Text(today.summary).appFont(.label)
            }
            Text("Every request the assistant sent off this Mac: when, where to, "
                 + "which engine or tool, how much, and whether personal text was in it.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            if !viewModel.limitNote.isEmpty {
                Text(viewModel.limitNote)
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            if viewModel.loaded && viewModel.rows.isEmpty {
                Text("No requests recorded.").appFont(.secondary).foregroundStyle(.secondary)
            }
            ForEach(visibleRows) { row in
                EgressRowView(row: row)
            }
            if viewModel.rows.count > 10 {
                Button(showAll ? "Show fewer" : "Show all \(viewModel.rows.count)") {
                    showAll.toggle()
                }
                .buttonStyle(.link)
            }
            Toggle("Keep request text (7 days)", isOn: Binding(
                get: { viewModel.keepText },
                set: { value in Task { await viewModel.setKeepText(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("keep_text"))
            if viewModel.isLocked("keep_text") {
                Text("Set by AGENT_EGRESS_KEEP_TEXT in the daemon's shell; unset it there to change it here.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            Text("Off: only sizes and destinations are kept, never what was said.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            HStack {
                Button("Refresh") { Task { await viewModel.refresh() } }
                Button("Clear ledger") { confirmClear = true }
                    .disabled(!viewModel.loaded || viewModel.rows.isEmpty)
            }
            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Privacy ledger")
        }
        .confirmationDialog("Clear the privacy ledger?", isPresented: $confirmClear) {
            Button("Clear", role: .destructive) { Task { await viewModel.clear() } }
        }
        .task { await viewModel.refresh() }
    }
}

private struct EgressRowView: View {
    let row: EgressRow
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                Text(row.shortTime).appFont(.fine).foregroundStyle(.secondary)
                Text(row.destination).appFont(.callout)
                Spacer()
                Text("\(egressBytes(row.bytesOut)) out · \(egressBytes(row.bytesIn)) in")
                    .appFont(.fine, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 6) {
                Text(row.what).appFont(.secondary)
                if let flag = row.personalText {
                    Text(flag == "sent" ? "personal text sent" : "personal text kept here")
                        .appFont(.fine)
                        .foregroundStyle(flag == "sent" ? Color.orange : Color.secondary)
                }
            }
            if let note = row.note {
                Text(note).appFont(.fine).foregroundStyle(.secondary)
            }
            if let text = row.text, !text.isEmpty {
                DisclosureGroup(isExpanded: $expanded) {
                    Text(text)
                        .appFont(.fine, design: .monospaced)
                        .textSelection(.enabled)
                        .lineLimit(expanded ? 40 : 1)
                } label: {
                    Text("Request text").appFont(.fine)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

/// One line for the menu and the Debug tab: "Nothing left this Mac today"
/// or "3 requests left this Mac today (…)". Fetches `GET /api/egress/stats`.
struct PrivacyLedgerTodayLabel: View {
    var baseURL: URL = URL(string: "http://127.0.0.1:8000")!
    @State private var summary: String?

    var body: some View {
        Group {
            if let summary {
                Label(summary, systemImage: "network")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
        }
        .task { await load() }
    }

    private func load() async {
        do {
            let request = try DaemonAuth.request(baseURL.appendingPathComponent("api/egress/stats"))
            let (data, response) = try await URLSession.shared.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                return
            }
            summary = try JSONDecoder().decode(EgressStats.self, from: data).summary
        } catch {
            summary = nil
        }
    }
}
