import SwiftUI

/// Response of `GET`/`PUT /api/meeting-prep` (#71).
private struct MeetingPrepSettings: Codable, Equatable {
    var enabled: Bool
    var minutesBefore: Int
    var minMinutes: Int
    var maxMinutes: Int
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled
        case minutesBefore = "minutes_before"
        case minMinutes = "min_minutes"
        case maxMinutes = "max_minutes"
        case lockedFields = "locked_fields"
    }
}

private struct MeetingPrepUpdate: Encodable {
    var enabled: Bool?
    var minutesBefore: Int?

    enum CodingKeys: String, CodingKey {
        case enabled
        case minutesBefore = "minutes_before"
    }
}

@MainActor
final class MeetingPrepSettingsViewModel: ObservableObject {
    @Published private(set) var enabled = true
    @Published private(set) var minutesBefore = 10
    @Published private(set) var minMinutes = 1
    @Published private(set) var maxMinutes = 10
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

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    /// Saved as soon as it is flipped: the daemon reads it for every meeting.
    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: MeetingPrepUpdate(enabled: value, minutesBefore: nil),
                   status: value ? "Meeting prep is on." : "Meeting prep is off.")
    }

    func setMinutes(_ value: Int) async {
        let clamped = min(max(value, minMinutes), maxMinutes)
        guard clamped != minutesBefore else { return }
        minutesBefore = clamped
        await send(method: "PUT", body: MeetingPrepUpdate(enabled: nil, minutesBefore: clamped),
                   status: "Meeting prep time saved.")
    }

    private func send(method: String, body: MeetingPrepUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/meeting-prep"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(MeetingPrepSettings.self, from: data))
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
            baseURL.appendingPathComponent("api/meeting-prep")
        ), let (data, _) = try? await session.data(for: request),
        let settings = try? JSONDecoder().decode(MeetingPrepSettings.self, from: data)
        else { return }
        apply(settings)
    }

    private func apply(_ settings: MeetingPrepSettings) {
        enabled = settings.enabled
        minutesBefore = settings.minutesBefore
        minMinutes = settings.minMinutes
        maxMinutes = settings.maxMinutes
        lockedFields = settings.lockedFields
        loaded = true
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "MeetingPrep", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "Meeting prep" section for Settings (#71). Embed inside a `Form` with
/// one line, next to the morning brief: `MeetingPrepSettingsView()`.
struct MeetingPrepSettingsView: View {
    @StateObject private var viewModel = MeetingPrepSettingsViewModel()

    var body: some View {
        Section {
            Toggle("Prepare me before meetings", isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_MEETING_PREP")

            Stepper(value: Binding(
                get: { viewModel.minutesBefore },
                set: { value in Task { await viewModel.setMinutes(value) } }
            ), in: viewModel.minMinutes...max(viewModel.minMinutes, viewModel.maxMinutes)) {
                Text("\(viewModel.minutesBefore) minutes before")
            }
            .disabled(!viewModel.loaded || !viewModel.enabled || viewModel.isLocked("minutes_before"))
            lockedNote("minutes_before", env: "AGENT_MEETING_PREP_MINUTES")

            Text("Before a meeting with other people: who is in it, your recent mail with them, "
                 + "files that match its title and a note with the same name. "
                 + "It only reads; nothing is sent or changed.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)

            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Meeting prep")
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
