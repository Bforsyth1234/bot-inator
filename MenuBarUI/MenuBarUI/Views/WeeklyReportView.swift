import SwiftUI

/// Response of `GET`/`PUT /api/weekly-report` (#128).
private struct WeeklyReportSettings: Codable, Equatable {
    var enabled: Bool
    var weekday: String         // "mon" ... "sun"
    var time: String            // "HH:MM", 24-hour, local
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, weekday, time
        case lockedFields = "locked_fields"
    }
}

private struct WeeklyReportUpdate: Encodable {
    var enabled: Bool?
    var weekday: String?
    var time: String?
}

/// One week of `GET /api/weekly-report/weeks`. The daemon builds `text`
/// from counts in its local stores; nothing here is worded by a model.
struct WeeklyReportWeek: Codable, Hashable, Identifiable {
    var id: Int { weeksAgo }
    let weeksAgo: Int
    let label: String
    let text: String
    let empty: Bool
    let unavailable: [String]

    enum CodingKeys: String, CodingKey {
        case label, text, empty, unavailable
        case weeksAgo = "weeks_ago"
    }

    /// The report without its heading line (the row shows the label itself).
    var lines: [String] {
        let all = text.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        return empty ? all : Array(all.dropFirst())
    }
}

private struct WeeklyReportWeeks: Codable {
    let weeks: [WeeklyReportWeek]
}

private struct WeeklyReportRunResponse: Codable {
    let status: String
    let sent: Bool
    let report: WeeklyReportWeek
}

@MainActor
final class WeeklyReportViewModel: ObservableObject {
    static let weekdays: [(id: String, name: String)] = [
        ("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"),
        ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday"),
    ]

    @Published private(set) var enabled = true
    @Published private(set) var weekday = "fri"
    @Published private(set) var time = WeeklyReportViewModel.date(from: "16:00")
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var weeks: [WeeklyReportWeek] = []
    @Published private(set) var loaded = false
    @Published private(set) var weeksLoaded = false
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
        await loadWeeks()
    }

    /// Saved as soon as it is changed: the daemon reads it on every check.
    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: WeeklyReportUpdate(enabled: value),
                   status: value ? "Weekly report is on." : "Weekly report is off.")
    }

    func setWeekday(_ value: String) async {
        guard value != weekday else { return }
        weekday = value
        await send(method: "PUT", body: WeeklyReportUpdate(weekday: value),
                   status: "Weekly report day saved.")
    }

    func setTime(_ value: Date) async {
        let text = Self.text(from: value)
        guard text != Self.text(from: time) else { return }
        time = value
        await send(method: "PUT", body: WeeklyReportUpdate(time: text),
                   status: "Weekly report time saved.")
    }

    /// "Run now": the daemon builds this week's report and, unless the week
    /// is empty, publishes it (a notification and a line in Thoughts).
    func runNow() async {
        isBusy = true
        defer { isBusy = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/weekly-report/run"), method: "POST"
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            let reply = try JSONDecoder().decode(WeeklyReportRunResponse.self, from: data)
            errorMessage = nil
            if !reply.sent {
                statusMessage = "Nothing to report for this week, so nothing was sent."
            } else if !reply.report.unavailable.isEmpty {
                statusMessage = "Report sent. Unavailable: \(reply.report.unavailable.joined(separator: ", "))."
            } else {
                statusMessage = "Report sent. See the Thoughts tab."
            }
        } catch {
            errorMessage = "Report failed: \(error.localizedDescription)"
            return
        }
        await loadWeeks()
    }

    func loadWeeks() async {
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/weekly-report/weeks")
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            weeks = try JSONDecoder().decode(WeeklyReportWeeks.self, from: data).weeks
            weeksLoaded = true
        } catch {
            errorMessage = "Couldn't load the reports: \(error.localizedDescription)"
        }
    }

    private func send(method: String, body: WeeklyReportUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/weekly-report"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(WeeklyReportSettings.self, from: data))
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
            baseURL.appendingPathComponent("api/weekly-report")
        ), let (data, _) = try? await session.data(for: request),
        let settings = try? JSONDecoder().decode(WeeklyReportSettings.self, from: data)
        else { return }
        apply(settings)
    }

    private func apply(_ settings: WeeklyReportSettings) {
        enabled = settings.enabled
        weekday = settings.weekday
        time = Self.date(from: settings.time)
        lockedFields = settings.lockedFields
        loaded = true
    }

    static func date(from text: String) -> Date {
        let parts = text.split(separator: ":").compactMap { Int($0) }
        var components = DateComponents()
        components.hour = parts.count == 2 ? parts[0] : 16
        components.minute = parts.count == 2 ? parts[1] : 0
        return Calendar.current.date(from: components) ?? Date()
    }

    static func text(from date: Date) -> String {
        let components = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", components.hour ?? 16, components.minute ?? 0)
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "WeeklyReport", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "Weekly report" section for Settings (#128): the switch, the day and
/// time, "Run now" and the last four weeks. Embed inside a `Form` with one
/// line: `WeeklyReportView()`.
struct WeeklyReportView: View {
    @StateObject private var viewModel = WeeklyReportViewModel()

    var body: some View {
        Section {
            Toggle("Send a weekly report", isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_WEEKLY_REPORT")

            Picker("Day", selection: Binding(
                get: { viewModel.weekday },
                set: { value in Task { await viewModel.setWeekday(value) } }
            )) {
                ForEach(WeeklyReportViewModel.weekdays, id: \.id) { day in
                    Text(day.name).tag(day.id)
                }
            }
            .disabled(!viewModel.loaded || !viewModel.enabled || viewModel.isLocked("weekday"))
            lockedNote("weekday", env: "AGENT_WEEKLY_REPORT_DAY")

            DatePicker("Time", selection: Binding(
                get: { viewModel.time },
                set: { value in Task { await viewModel.setTime(value) } }
            ), displayedComponents: .hourAndMinute)
            .disabled(!viewModel.loaded || !viewModel.enabled || viewModel.isLocked("time"))
            lockedNote("time", env: "AGENT_WEEKLY_REPORT_TIME")

            Text("Once a week: what the assistant did in the last 7 days, as counts from History and "
                 + "your answers to its cards. Built on this Mac without a model; nothing is sent or changed. "
                 + "A week with nothing in it sends no report.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)

            HStack {
                Button("Run now") { Task { await viewModel.runNow() } }
                    .disabled(!viewModel.loaded || viewModel.isBusy)
                if viewModel.isBusy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Refresh") { Task { await viewModel.loadWeeks() } }
                    .buttonStyle(.borderless)
                    .appFont(.secondary)
            }
            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }

            if viewModel.weeksLoaded && viewModel.weeks.isEmpty {
                Text("No reports yet.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            ForEach(viewModel.weeks) { week in
                weekRow(week)
            }
        } header: {
            Text("Weekly report")
        }
        .task { await viewModel.refresh() }
    }

    private func weekRow(_ week: WeeklyReportWeek) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(Self.title(week))
                .appFont(.secondary, weight: .semibold)
            ForEach(Array(week.lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .appFont(.secondary)
                    .foregroundStyle(Self.isWarning(line) ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .padding(.vertical, 2)
    }

    private static func title(_ week: WeeklyReportWeek) -> String {
        switch week.weeksAgo {
        case 0: return "Last 7 days (\(week.label))"
        case 1: return "The week before (\(week.label))"
        default: return "\(week.weeksAgo) weeks before (\(week.label))"
        }
    }

    private static func isWarning(_ line: String) -> Bool {
        line.contains(": unavailable:") || line.contains("Unavailable:")
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
