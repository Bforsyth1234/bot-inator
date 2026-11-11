import SwiftUI

/// One row of `GET /api/permissions` (#59).
struct PermissionRow: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let why: String
    let usedBy: [String]
    let requestKind: String   // "request" | "settings" | "instructions"
    let status: String        // "granted" | "denied" | "not_asked" | "unknown"
    let detail: String

    enum CodingKeys: String, CodingKey {
        case id, name, why, status, detail
        case usedBy = "used_by"
        case requestKind = "request_kind"
    }
}

struct PermissionsProcess: Codable, Equatable {
    let executable: String
    let responsibleProcess: String?
    let note: String

    enum CodingKeys: String, CodingKey {
        case executable, note
        case responsibleProcess = "responsible_process"
    }
}

private struct PermissionsResponse: Codable {
    let permissions: [PermissionRow]
    let process: PermissionsProcess
}

private struct PermissionRequestResponse: Codable {
    let action: String?
    let detail: String?
    let permission: PermissionRow
}

@MainActor
final class PermissionsViewModel: ObservableObject {
    @Published private(set) var rows: [PermissionRow] = []
    @Published private(set) var process: PermissionsProcess?
    @Published private(set) var isBusy = false
    @Published private(set) var pending: String?
    @Published var errorMessage: String?
    @Published var messages: [String: String] = [:]

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Checks only; the daemon never prompts on GET.
    func refresh() async {
        isBusy = true
        defer { isBusy = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/permissions")
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            let decoded = try JSONDecoder().decode(PermissionsResponse.self, from: data)
            rows = decoded.permissions
            process = decoded.process
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Only on a click: may show a macOS prompt or open System Settings.
    func request(_ row: PermissionRow) async {
        pending = row.id
        defer { pending = nil }
        // #99: only the app can ask for these; the daemon could only open
        // System Settings, where an app that never asked isn't listed.
        if let kind = MeetingPermissionKind(rawValue: row.id) {
            let after = await MeetingPermissions.request(kind)
            messages[row.id] = Self.describe(after)
            // The recorder reports the new status over the WebSocket.
            try? await Task.sleep(for: .milliseconds(500))
            await refresh()
            return
        }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/permissions/\(row.id)/request"),
                method: "POST"
            )
            // An Automation prompt blocks until the user answers it.
            request.timeoutInterval = 180
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            let decoded = try JSONDecoder().decode(PermissionRequestResponse.self, from: data)
            if let index = rows.firstIndex(where: { $0.id == row.id }) {
                rows[index] = decoded.permission
            }
            messages[row.id] = decoded.detail
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private static func describe(_ status: MeetingAuthStatus) -> String {
        switch status {
        case .authorized: return "granted"
        case .notDetermined: return "not asked yet"
        case .denied, .restricted:
            return "turned off: switch on MenuBarUI in the System Settings pane that just opened"
        }
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "Permissions", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "Permissions" section for Settings. Embed inside a `Form` with one line:
/// `PermissionsView()`.
struct PermissionsView: View {
    @StateObject private var viewModel = PermissionsViewModel()

    var body: some View {
        Section {
            ForEach(viewModel.rows) { row in
                rowView(row)
            }
            if let process = viewModel.process {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Grant to: \(process.responsibleProcess ?? process.executable)")
                    if process.responsibleProcess != nil,
                       process.responsibleProcess != process.executable {
                        Text("Daemon binary: \(process.executable)")
                    }
                    Text(process.note)
                }
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            HStack {
                Text("Permissions")
                Spacer()
                if viewModel.isBusy { ProgressView().controlSize(.small) }
                Button {
                    Task { await viewModel.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Check every permission again (never prompts)")
                .disabled(viewModel.isBusy)
            }
        }
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func rowView(_ row: PermissionRow) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(color(row.status))
                .frame(width: 9, height: 9)
                .padding(.top, 4)
                .help(label(row.status))
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name)
                Text(row.why).appFont(.secondary).foregroundStyle(.secondary)
                if let note = viewModel.messages[row.id] ?? (row.detail.isEmpty ? nil : row.detail) {
                    Text(note).appFont(.secondary).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if row.status != "granted" {
                if viewModel.pending == row.id {
                    ProgressView().controlSize(.small)
                } else {
                    Button(buttonTitle(row)) {
                        Task { await viewModel.request(row) }
                    }
                    .disabled(viewModel.pending != nil)
                }
            }
        }
    }

    private func buttonTitle(_ row: PermissionRow) -> String {
        // #99: the app asks for these itself (a real prompt the first time).
        if MeetingPermissionKind(rawValue: row.id) != nil {
            return row.status == "denied" ? "Open Settings" : "Request"
        }
        switch row.requestKind {
        case "settings": return "Open Settings"
        case "instructions": return "How?"
        default: return row.status == "denied" ? "Open Settings" : "Request"
        }
    }

    private func color(_ status: String) -> Color {
        switch status {
        case "granted": return .green
        case "denied": return .red
        case "not_asked": return .yellow
        default: return .gray
        }
    }

    private func label(_ status: String) -> String {
        switch status {
        case "granted": return "Granted"
        case "denied": return "Denied"
        case "not_asked": return "Not asked yet"
        default: return "Unknown"
        }
    }
}
