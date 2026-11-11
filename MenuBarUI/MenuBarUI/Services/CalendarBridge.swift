import Foundation
import EventKit

/// EventKit on behalf of the daemon (#60).
///
/// The daemon's Python can't get the Calendar prompt (a bare interpreter has
/// no Info.plist usage string), so the app owns EventKit and answers the
/// daemon's `calendar_request` frames. Access is requested only when the
/// daemon first needs it (never at launch). All EventKit work runs on a
/// private serial queue, off the main thread.
final class CalendarBridge: @unchecked Sendable {
    static let notesLimit = 500

    private let store = EKEventStore()
    private let queue = DispatchQueue(label: "botinator.calendar-bridge", qos: .userInitiated)

    /// Current authorization. Reading it never prompts.
    static func authorization() -> CalendarAuthorization {
        switch EKEventStore.authorizationStatus(for: .event) {
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        case .denied: return .denied
        case .fullAccess: return .fullAccess
        case .writeOnly: return .writeOnly
        @unknown default: return .denied
        }
    }

    private struct BridgeError: Error {
        let message: String
    }

    // MARK: - Entry point

    func handle(_ request: CalendarRequestPayload) async -> CalendarResponsePayload {
        do {
            var response: CalendarResponsePayload
            switch request.op {
            case .list:
                try await ensureAccess(write: false)
                let events = try await list(args: request.args)
                response = CalendarResponsePayload(requestId: request.requestId, ok: true, events: events)
            case .create:
                try await ensureAccess(write: true)
                let (eventId, calendar) = try await create(args: request.args)
                response = CalendarResponsePayload(requestId: request.requestId, ok: true,
                                                   eventId: eventId, calendar: calendar)
            case .requestAccess:
                try await ensureAccess(write: false)
                response = CalendarResponsePayload(requestId: request.requestId, ok: true)
            }
            response.authorization = Self.authorization()
            return response
        } catch let error as BridgeError {
            return failure(request, error.message)
        } catch {
            return failure(request, "EventKit error: \(error.localizedDescription)")
        }
    }

    private func failure(_ request: CalendarRequestPayload, _ message: String) -> CalendarResponsePayload {
        CalendarResponsePayload(requestId: request.requestId, ok: false,
                                error: message, authorization: Self.authorization())
    }

    // MARK: - Access

    private func ensureAccess(write: Bool) async throws {
        var status = Self.authorization()
        if status == .notDetermined {
            // Shows the macOS "allow access to Calendar" prompt (macOS 14+).
            let granted = (try? await store.requestFullAccessToEvents()) ?? false
            status = Self.authorization()
            if granted {
                try await onQueue { [store] in store.reset() }  // pick up the calendars now visible
            }
        }
        switch status {
        case .fullAccess:
            return
        case .writeOnly where write:
            return
        case .writeOnly:
            throw BridgeError(message: "The bot-inator menu bar app can only add events, not read "
                + "them. Allow full access in System Settings > Privacy & Security > Calendars.")
        case .restricted:
            throw BridgeError(message: "Calendar access is restricted on this Mac (Screen Time "
                + "or a device profile), so the bot-inator menu bar app can't read calendars.")
        case .denied, .notDetermined:
            throw BridgeError(message: "Calendar access was denied for the bot-inator menu bar "
                + "app. Allow it in System Settings > Privacy & Security > Calendars.")
        }
    }

    // MARK: - Operations

    private func onQueue<T>(_ work: @escaping () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do { continuation.resume(returning: try work()) } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func list(args: JSONValue?) async throws -> [CalendarEventDTO] {
        let start = try Self.date(args, "start")
        let end = try Self.date(args, "end")
        let names = Self.strings(args, "calendars")
        return try await onQueue { [store] in
            var calendars: [EKCalendar]? = nil
            if !names.isEmpty {
                calendars = store.calendars(for: .event).filter { names.contains($0.title) }
                if calendars?.isEmpty == true { return [] }
            }
            // The predicate expands recurring events into occurrences.
            let predicate = store.predicateForEvents(withStart: start, end: end, calendars: calendars)
            return store.events(matching: predicate)
                .sorted { $0.startDate < $1.startDate }
                .map(Self.dto)
        }
    }

    private func create(args: JSONValue?) async throws -> (String, String) {
        guard let title = Self.string(args, "title"), !title.isEmpty else {
            throw BridgeError(message: "title is required.")
        }
        let start = try Self.date(args, "start")
        let end = try Self.date(args, "end")
        let calendarName = Self.string(args, "calendar") ?? ""
        let location = Self.string(args, "location") ?? ""
        let notes = Self.string(args, "notes") ?? ""
        return try await onQueue { [store] in
            let calendar: EKCalendar
            if !calendarName.isEmpty {
                guard let named = store.calendars(for: .event).first(where: { $0.title == calendarName }) else {
                    throw BridgeError(message: "No calendar named '\(calendarName)'.")
                }
                calendar = named
            } else {
                guard let fallback = store.defaultCalendarForNewEvents else {
                    throw BridgeError(message: "No default calendar for new events.")
                }
                calendar = fallback
            }
            guard calendar.allowsContentModifications else {
                throw BridgeError(message: "Calendar '\(calendar.title)' is read-only.")
            }
            let event = EKEvent(eventStore: store)
            event.title = title
            event.startDate = start
            event.endDate = end
            event.calendar = calendar
            if !location.isEmpty { event.location = location }
            if !notes.isEmpty { event.notes = notes }
            try store.save(event, span: .thisEvent, commit: true)
            return (event.eventIdentifier ?? "", calendar.title)
        }
    }

    // MARK: - Mapping

    private static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = .current
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }

    private static func dto(_ event: EKEvent) -> CalendarEventDTO {
        CalendarEventDTO(
            id: event.eventIdentifier ?? "",
            title: event.title ?? "",
            start: iso(event.startDate),
            end: iso(event.endDate),
            calendar: event.calendar?.title ?? "",
            location: event.location ?? "",
            notes: String((event.notes ?? "").prefix(notesLimit)),
            url: event.url?.absoluteString ?? "",
            allDay: event.isAllDay,
            attendees: attendees(event)
        )
    }

    static let attendeesLimit = 50

    /// `EKEvent.attendees` plus the organizer when not listed (#80); `[]`
    /// when the event has no other people. Rooms and resources are skipped.
    private static func attendees(_ event: EKEvent) -> [CalendarAttendeeDTO] {
        var people = (event.attendees ?? []).compactMap(attendee)
        if !people.isEmpty, let organizer = event.organizer.flatMap(attendee),
           !people.contains(where: { !$0.email.isEmpty && $0.email == organizer.email }) {
            people.append(organizer)
        }
        return Array(people.prefix(attendeesLimit))
    }

    private static func attendee(_ participant: EKParticipant) -> CalendarAttendeeDTO? {
        switch participant.participantType {
        case .room, .resource: return nil
        default: break
        }
        var email = ""
        let url = participant.url
        if url.scheme?.lowercased() == "mailto" {
            let raw = url.absoluteString.dropFirst("mailto:".count)
            let address = raw.split(separator: "?", maxSplits: 1).first.map(String.init) ?? ""
            email = (address.removingPercentEncoding ?? address).lowercased()
        }
        let name = participant.name ?? ""
        if name.isEmpty && email.isEmpty { return nil }
        return CalendarAttendeeDTO(name: name, email: email, isSelf: participant.isCurrentUser)
    }

    // MARK: - Args

    private static func field(_ args: JSONValue?, _ key: String) -> JSONValue? {
        if case .object(let dict)? = args { return dict[key] }
        return nil
    }

    private static func string(_ args: JSONValue?, _ key: String) -> String? {
        if case .string(let value)? = field(args, key) { return value }
        return nil
    }

    private static func strings(_ args: JSONValue?, _ key: String) -> [String] {
        guard case .array(let items)? = field(args, key) else { return [] }
        return items.compactMap { if case .string(let s) = $0 { return s } else { return nil } }
    }

    private static func date(_ args: JSONValue?, _ key: String) throws -> Date {
        guard let raw = string(args, key) else { throw BridgeError(message: "\(key) is required.") }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: raw) { return d }
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: raw) { return d }
        throw BridgeError(message: "Bad date for \(key): \(raw)")
    }
}
