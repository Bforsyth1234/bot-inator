import SwiftUI

/// Response of `GET`/`PUT /api/remote` (#85).
private struct RemoteSettings: Codable, Equatable {
    var enabled: Bool
    var handle: String
    var idleMinutes: Int
    var minIdleMinutes: Int
    var maxIdleMinutes: Int
    var timeoutMinutes: Int
    var maxPerHour: Int
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, handle
        case idleMinutes = "idle_minutes"
        case minIdleMinutes = "min_idle_minutes"
        case maxIdleMinutes = "max_idle_minutes"
        case timeoutMinutes = "timeout_minutes"
        case maxPerHour = "max_per_hour"
        case lockedFields = "locked_fields"
    }
}

private struct RemoteUpdate: Encodable {
    var enabled: Bool?
    var handle: String?
    var idleMinutes: Int?

    enum CodingKeys: String, CodingKey {
        case enabled, handle
        case idleMinutes = "idle_minutes"
    }
}

@MainActor
final class RemoteSettingsViewModel: ObservableObject {
    @Published private(set) var enabled = false
    @Published private(set) var handle = ""
    @Published var handleDraft = ""
    @Published private(set) var idleMinutes = 10
    @Published private(set) var minIdleMinutes = 1
    @Published private(set) var maxIdleMinutes = 120
    @Published private(set) var timeoutMinutes = 15
    @Published private(set) var maxPerHour = 20
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var loaded = false
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

    func isLocked(_ field: String) -> Bool { lockedFields.contains(field) }

    var handleChanged: Bool {
        handleDraft.trimmingCharacters(in: .whitespaces) != handle
    }

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: RemoteUpdate(enabled: value),
                   status: value ? "iPhone remote is on." : "iPhone remote is off.")
    }

    func saveHandle() async {
        let value = handleDraft.trimmingCharacters(in: .whitespaces)
        await send(method: "PUT", body: RemoteUpdate(handle: value),
                   status: value.isEmpty ? "Handle removed; the remote is off." : "Handle saved.")
    }

    func setIdleMinutes(_ value: Int) async {
        let clamped = min(max(value, minIdleMinutes), maxIdleMinutes)
        guard clamped != idleMinutes else { return }
        idleMinutes = clamped
        await send(method: "PUT", body: RemoteUpdate(idleMinutes: clamped),
                   status: "Idle time saved.")
    }

    private func send(method: String, body: RemoteUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/remote"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(RemoteSettings.self, from: data))
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (body == nil ? "" : "Save failed: ") + error.localizedDescription
            if body != nil { await reload() }
        }
    }

    /// After a failed save, show what the daemon actually has.
    private func reload() async {
        guard let request = try? DaemonAuth.request(
            baseURL.appendingPathComponent("api/remote")
        ), let (data, _) = try? await session.data(for: request),
        let settings = try? JSONDecoder().decode(RemoteSettings.self, from: data)
        else { return }
        apply(settings)
    }

    private func apply(_ settings: RemoteSettings) {
        enabled = settings.enabled
        handle = settings.handle
        handleDraft = settings.handle
        idleMinutes = settings.idleMinutes
        minIdleMinutes = settings.minIdleMinutes
        maxIdleMinutes = settings.maxIdleMinutes
        timeoutMinutes = settings.timeoutMinutes
        maxPerHour = settings.maxPerHour
        lockedFields = settings.lockedFields
        loaded = true
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "Remote", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "iPhone remote" section for Settings (#85). Embed inside a `Form` with
/// one line: `RemoteSettingsView()`.
struct RemoteSettingsView: View {
    @StateObject private var viewModel = RemoteSettingsViewModel()

    var body: some View {
        Section {
            HStack {
                TextField("Your phone number or Apple ID", text: $viewModel.handleDraft)
                    .appFont(.body)
                    .textFieldStyle(.roundedBorder)
                    .disabled(!viewModel.loaded || viewModel.isLocked("handle"))
                    .onSubmit { Task { await viewModel.saveHandle() } }
                Button("Save") { Task { await viewModel.saveHandle() } }
                    .appFont(.label)
                    .disabled(!viewModel.loaded || viewModel.isBusy || !viewModel.handleChanged
                              || viewModel.isLocked("handle"))
            }
            lockedNote("handle", env: "AGENT_REMOTE_HANDLE")

            Toggle(isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            )) {
                Text("Ask and approve from my Messages thread").appFont(.body)
            }
            .disabled(!viewModel.loaded || viewModel.handle.isEmpty || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_REMOTE")

            Stepper(value: Binding(
                get: { viewModel.idleMinutes },
                set: { value in Task { await viewModel.setIdleMinutes(value) } }
            ), in: viewModel.minIdleMinutes...max(viewModel.minIdleMinutes, viewModel.maxIdleMinutes)) {
                Text("Send cards to the phone after \(viewModel.idleMinutes) min away from the Mac")
                    .appFont(.body)
            }
            .disabled(!viewModel.loaded || !viewModel.enabled || viewModel.isLocked("idle_minutes"))
            lockedNote("idle_minutes", env: "AGENT_REMOTE_IDLE_MINUTES")

            Text("Text your own iMessage handle from your iPhone to chat with the assistant; "
                 + "answers come back in that thread. While the Mac is idle, or when you asked "
                 + "from the phone, a card is sent there with a code: reply \"yes CODE\" or "
                 + "\"no CODE\" within \(viewModel.timeoutMinutes) minutes. Code review, new "
                 + "tools, shortcuts and cards shaped by an email or web page still wait for "
                 + "the Mac. Messages go only to this handle, through Apple's iMessage service, "
                 + "at most \(viewModel.maxPerHour) an hour, and each is listed in What left this Mac.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)

            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("iPhone remote").appFont(.headline)
        }
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func lockedNote(_ field: String, env: String) -> some View {
        if viewModel.isLocked(field) {
            Text("Set by \(env) in the daemon's shell; unset it there to change it here.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        }
    }
}
