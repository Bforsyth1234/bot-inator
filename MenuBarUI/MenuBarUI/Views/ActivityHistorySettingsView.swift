import SwiftUI

/// Response of `GET`/`PUT /api/activity` (#75).
private struct ActivitySettings: Codable, Equatable {
    var enabled: Bool
    var count: Int
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, count
        case lockedFields = "locked_fields"
    }
}

private struct ActivityUpdate: Encodable {
    var enabled: Bool
}

@MainActor
final class ActivityHistorySettingsViewModel: ObservableObject {
    @Published private(set) var enabled = true
    @Published private(set) var count = 0
    @Published private(set) var lockedFields: [String] = []
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

    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: ActivityUpdate(enabled: value),
                   status: value ? "Activity history is on." : "Activity history is off.")
    }

    func clear() async {
        await send(method: "DELETE", body: nil, status: "Activity history cleared.")
    }

    private func send(method: String, body: ActivityUpdate?, status: String?) async {
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/activity"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                throw NSError(domain: "ActivityHistory", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
            }
            let settings = try JSONDecoder().decode(ActivitySettings.self, from: data)
            enabled = settings.enabled
            count = settings.count
            lockedFields = settings.lockedFields
            loaded = true
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (method == "GET" ? "" : "Save failed: ") + error.localizedDescription
        }
    }
}

/// "Activity history" section for Settings (#75).
struct ActivityHistorySettingsView: View {
    @StateObject private var viewModel = ActivityHistorySettingsViewModel()
    @State private var confirmClear = false

    var body: some View {
        Section {
            Toggle("Remember activity history (on this Mac)", isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            if viewModel.isLocked("enabled") {
                Text("Set by AGENT_ACTIVITY_HISTORY in the daemon's shell; unset it there to change it here.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            Text("Keeps which apps you open and where files land, never their contents, "
                 + "for 90 days, to suggest routines. \(viewModel.count) events stored.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            Button("Clear activity history") { confirmClear = true }
                .disabled(!viewModel.loaded || viewModel.count == 0)
            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Activity history")
        }
        .confirmationDialog("Clear all activity history?", isPresented: $confirmClear) {
            Button("Clear", role: .destructive) { Task { await viewModel.clear() } }
        }
        .task { await viewModel.refresh() }
    }
}
