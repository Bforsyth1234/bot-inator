import Foundation
import Contacts

/// Contacts on behalf of the daemon (#127).
///
/// The daemon's Python can't get the Contacts prompt (a bare interpreter has
/// no Info.plist usage string), so the app owns `CNContactStore` and answers
/// the daemon's `contacts_request` frames, as `CalendarBridge` does for
/// EventKit (#60). Access is requested only when the daemon first needs it
/// (never at launch). All Contacts work runs on a private serial queue, off
/// the main thread.
///
/// Read-only, and narrow by construction: `keysToFetch` is the whole list
/// of what is ever read from a contact (name parts, email addresses, phone
/// numbers, organisation). Notes, birthdays, postal addresses and photos
/// are never requested, so they cannot be sent. There is no "list all": a
/// lookup needs a name of at least `minNameChars` characters and returns at
/// most `maxLimit` contacts. Nothing is created, changed or deleted.
final class ContactsBridge: @unchecked Sendable {
    static let minNameChars = 2
    static let defaultLimit = 5
    static let maxLimit = 10
    static let maxValuesPerField = 10

    /// Everything this bridge reads from a contact.
    static let keysToFetch: [CNKeyDescriptor] = [
        CNContactGivenNameKey, CNContactMiddleNameKey, CNContactFamilyNameKey,
        CNContactNicknameKey, CNContactOrganizationNameKey,
        CNContactEmailAddressesKey, CNContactPhoneNumbersKey,
    ].map { $0 as CNKeyDescriptor }

    private let store = CNContactStore()
    private let queue = DispatchQueue(label: "botinator.contacts-bridge", qos: .userInitiated)

    /// Current authorization. Reading it never prompts.
    static func authorization() -> ContactsAuthorization {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        case .denied: return .denied
        case .authorized: return .authorized
        @unknown default: return .denied
        }
    }

    private struct BridgeError: Error {
        let message: String
    }

    // MARK: - Entry point

    func handle(_ request: ContactsRequestPayload) async -> ContactsResponsePayload {
        do {
            try await ensureAccess()
            var response = ContactsResponsePayload(requestId: request.requestId, ok: true)
            if (request.op ?? .find) == .find {
                let (contacts, truncated) = try await find(name: request.name ?? "",
                                                           limit: request.limit ?? Self.defaultLimit)
                response.contacts = contacts
                response.truncated = truncated
            }
            response.authorization = Self.authorization()
            return response
        } catch let error as BridgeError {
            return failure(request, error.message)
        } catch {
            return failure(request, "Contacts error: \(error.localizedDescription)")
        }
    }

    private func failure(_ request: ContactsRequestPayload, _ message: String) -> ContactsResponsePayload {
        ContactsResponsePayload(requestId: request.requestId, ok: false,
                                error: message, authorization: Self.authorization())
    }

    // MARK: - Access

    private func ensureAccess() async throws {
        var status = Self.authorization()
        if status == .notDetermined {
            // Shows the macOS "allow access to Contacts" prompt.
            _ = try? await store.requestAccess(for: .contacts)
            status = Self.authorization()
        }
        switch status {
        case .authorized:
            return
        case .restricted:
            throw BridgeError(message: "Contacts access is restricted on this Mac (Screen Time "
                + "or a device profile), so the bot-inator menu bar app can't look up contacts.")
        case .denied, .notDetermined, .limited:
            throw BridgeError(message: "Contacts access is off for the bot-inator menu bar "
                + "app. Allow it in System Settings > Privacy & Security > Contacts.")
        }
    }

    // MARK: - Lookup

    private func onQueue<T>(_ work: @escaping () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do { continuation.resume(returning: try work()) } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func find(name: String, limit: Int) async throws -> ([ContactDTO], Bool) {
        let query = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= Self.minNameChars else {
            throw BridgeError(message: "name must be at least \(Self.minNameChars) characters.")
        }
        let cap = max(1, min(limit, Self.maxLimit))
        return try await onQueue { [store] in
            let predicate = CNContact.predicateForContacts(matchingName: query)
            let found = try store.unifiedContacts(matching: predicate, keysToFetch: Self.keysToFetch)
            let all = found.map(Self.dto)
                .filter { !$0.name.isEmpty || !$0.emails.isEmpty || !$0.phones.isEmpty }
                .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            return (Array(all.prefix(cap)), all.count > cap)
        }
    }

    /// Only the keys in `keysToFetch` may be touched here: reading any other
    /// property of a partially fetched contact raises.
    private static func dto(_ contact: CNContact) -> ContactDTO {
        let person = [contact.givenName, contact.middleName, contact.familyName]
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        let organization = contact.organizationName.trimmingCharacters(in: .whitespaces)
        let nickname = contact.nickname.trimmingCharacters(in: .whitespaces)
        let name = !person.isEmpty ? person : (!nickname.isEmpty ? nickname : organization)
        return ContactDTO(
            name: name,
            emails: unique(contact.emailAddresses.map { $0.value as String }),
            phones: unique(contact.phoneNumbers.map { $0.value.stringValue }),
            organization: organization
        )
    }

    private static func unique(_ values: [String]) -> [String] {
        var out: [String] = []
        for value in values {
            let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty && !out.contains(text) { out.append(text) }
            if out.count >= maxValuesPerField { break }
        }
        return out
    }
}
