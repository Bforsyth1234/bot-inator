import AppKit
import ApplicationServices

/// The front window's Accessibility text, on the daemon's request (#89).
///
/// The daemon's Python has no Accessibility (TCC) grant, so the app reads
/// the window and answers `screen_text_request` frames, as `CalendarBridge`
/// does for EventKit (#60). Rules (#89 decisions):
///
/// - Accessibility text only: no screenshot, no OCR, nothing stored. The
///   walk is capped (depth, nodes, `maxChars`, `timeBudget`); a window with
///   no AX text answers `nothing_readable`, never a guess.
/// - Never read: secure text fields (skipped, and nothing at all is read
///   while one has focus), the never-watch apps the daemon lists, this
///   app's own windows, and browser windows (the daemon's
///   `read_active_tab_text` applies the #58 sensitive-site rules there).
/// - Never prompts: `AXIsProcessTrusted()` only checks. The user grants
///   Accessibility from quick-ask's "Allow" button or System Settings.
///
/// When this app is in front (the Chat window), the window read is the
/// focused window of the app that was active just before it.
final class ScreenTextBridge: @unchecked Sendable {
    static let maxChars = 8000
    static let maxDepth = 40
    static let maxNodes = 4000
    static let timeBudget: TimeInterval = 1.0
    /// Per AX call: a hung app must not hang the read.
    static let messagingTimeout: Float = 0.25

    /// Same list as the daemon's `tools.read_front_window.BROWSER_BUNDLE_PREFIXES`.
    static let browserBundlePrefixes: [String] = [
        "com.apple.safari", "com.google.chrome", "org.chromium.chromium",
        "org.mozilla.", "company.thebrowser.", "com.brave.browser",
        "com.microsoft.edgemac", "com.operasoftware.", "com.vivaldi.vivaldi",
        "app.zen-browser.", "org.torproject.", "com.kagi.kagimacos",
    ]

    /// Roles whose `AXValue` is the text shown (children not walked).
    private static let valueRoles: Set<String> = [
        kAXStaticTextRole as String, kAXTextAreaRole as String,
        kAXTextFieldRole as String, kAXComboBoxRole as String,
    ]
    private static let secureMarker = "AXSecureTextField"

    private let queue = DispatchQueue(label: "botinator.screen-text-bridge", qos: .userInitiated)
    private let lock = NSLock()
    private var lastOtherPid: pid_t?
    private var activationObserver: NSObjectProtocol?

    init() {
        let ownBundle = Bundle.main.bundleIdentifier
        if let front = NSWorkspace.shared.frontmostApplication,
           front.bundleIdentifier != ownBundle {
            lastOtherPid = front.processIdentifier
        }
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
        ) { [weak self] note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.bundleIdentifier != ownBundle else { return }
            self?.setLastOther(app.processIdentifier)
        }
    }

    deinit {
        if let activationObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(activationObserver)
        }
    }

    /// Never prompts.
    static func isTrusted() -> Bool { AXIsProcessTrusted() }

    private func setLastOther(_ pid: pid_t) {
        lock.lock(); lastOtherPid = pid; lock.unlock()
    }

    private func lastOther() -> pid_t? {
        lock.lock(); defer { lock.unlock() }
        return lastOtherPid
    }

    // MARK: - Entry points

    func handle(_ request: ScreenTextRequestPayload) async -> ScreenTextResponsePayload {
        let limit = max(1, min(request.maxChars ?? Self.maxChars, Self.maxChars))
        return await read(requestId: request.requestId, maxChars: limit,
                          neverWatch: request.neverWatch ?? [])
    }

    /// One read of the front window. Also usable by quick-ask's "include
    /// this window's text" (pass the never-watch list the daemon sent, or []).
    func read(requestId: String = "", maxChars: Int = ScreenTextBridge.maxChars,
              neverWatch: [String]) async -> ScreenTextResponsePayload {
        let trusted = Self.isTrusted()
        guard trusted else {
            return Self.failure(requestId, .accessibilityOff,
                                "Accessibility is off for the bot-inator menu bar app.", trusted: false)
        }
        guard let app = targetApp() else {
            return Self.failure(requestId, .noWindow, "No window of another app is in front.", trusted: true)
        }
        let name = app.localizedName ?? ""
        let bundle = app.bundleIdentifier ?? ""
        if Self.isNeverWatched(bundle: bundle, name: name, list: neverWatch) {
            // App and title stay out too.
            return Self.failure(requestId, .neverWatch, "The front app is on the never-watch list.",
                                trusted: true)
        }
        if bundle == Bundle.main.bundleIdentifier || bundle.lowercased().hasPrefix("com.botinator.") {
            return Self.failure(requestId, .ownApp, "The front window is bot-inator's own.", trusted: true)
        }
        if Self.isBrowser(bundle) {
            var r = Self.failure(requestId, .browser, "Browser windows are read with read_active_tab_text.",
                                 trusted: true)
            r.app = name
            r.bundleId = bundle
            return r
        }
        let pid = app.processIdentifier
        let limit = max(1, min(maxChars, Self.maxChars))
        return await withCheckedContinuation { continuation in
            queue.async {
                var result = Self.walk(pid: pid, limit: limit, requestId: requestId)
                result.app = name
                result.bundleId = bundle
                continuation.resume(returning: result)
            }
        }
    }

    // MARK: - Target

    private func targetApp() -> NSRunningApplication? {
        let own = Bundle.main.bundleIdentifier
        if let front = NSWorkspace.shared.frontmostApplication, front.bundleIdentifier != own {
            return front
        }
        guard let pid = lastOther(),
              let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else { return nil }
        return app
    }

    static func isBrowser(_ bundle: String) -> Bool {
        let low = bundle.lowercased()
        return !low.isEmpty && browserBundlePrefixes.contains { low.hasPrefix($0) }
    }

    /// Entries with a dot are bundle ids, without one app names (as #87).
    static func isNeverWatched(bundle: String, name: String, list: [String]) -> Bool {
        let bid = bundle.lowercased()
        let app = name.lowercased()
        for entry in list {
            let low = entry.trimmingCharacters(in: .whitespaces).lowercased()
            if low.isEmpty { continue }
            if low.contains(".") {
                if !bid.isEmpty && bid == low { return true }
            } else if !app.isEmpty && app == low {
                return true
            }
        }
        return false
    }

    // MARK: - The walk (off the main thread)

    private static func walk(pid: pid_t, limit: Int, requestId: String) -> ScreenTextResponsePayload {
        let appElement = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(appElement, messagingTimeout)
        // A password field has focus: read nothing at all.
        if let focused: AXUIElement = attribute(appElement, kAXFocusedUIElementAttribute),
           isSecure(focused) {
            return failure(requestId, .secureField, "A password field has focus.", trusted: true)
        }
        guard let window: AXUIElement = attribute(appElement, kAXFocusedWindowAttribute)
                ?? attribute(appElement, kAXMainWindowAttribute) else {
            return failure(requestId, .noWindow, "The front app has no focused window.", trusted: true)
        }
        let title = nonEmpty(attribute(window, kAXTitleAttribute) as String?)

        let deadline = Date().addingTimeInterval(timeBudget)
        var pieces: [String] = []
        var total = 0
        var nodes = 0
        var truncated = false
        var stack: [(AXUIElement, Int)] = [(window, 0)]

        while let (element, depth) = stack.popLast() {
            if total >= limit || nodes >= maxNodes || Date() >= deadline {
                truncated = true
                break
            }
            nodes += 1
            AXUIElementSetMessagingTimeout(element, messagingTimeout)
            if isSecure(element) { continue }  // never its value, never its children
            let role = (attribute(element, kAXRoleAttribute) as String?) ?? ""
            if valueRoles.contains(role) {
                if let value = nonEmpty(attribute(element, kAXValueAttribute) as String?) {
                    let piece = value.trimmingCharacters(in: .whitespacesAndNewlines)
                    if piece != pieces.last {
                        let room = limit - total
                        if piece.count > room {
                            pieces.append(String(piece.prefix(room)))
                            total = limit
                            truncated = true
                        } else {
                            pieces.append(piece)
                            total += piece.count + 1
                        }
                    }
                }
                continue
            }
            guard depth < maxDepth else {
                truncated = true
                continue
            }
            if let children: [AXUIElement] = children(element) {
                // Reversed so the walk reads top to bottom.
                for child in children.reversed() { stack.append((child, depth + 1)) }
            }
        }

        let text = String(pieces.joined(separator: "\n").prefix(limit))
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            var r = failure(requestId, .nothingReadable, "This window exposes no Accessibility text.",
                            trusted: true)
            r.title = title
            return r
        }
        return ScreenTextResponsePayload(requestId: requestId, ok: true, title: title, text: text,
                                         truncated: truncated, accessibilityTrusted: true)
    }

    private static func isSecure(_ element: AXUIElement) -> Bool {
        let role = (attribute(element, kAXRoleAttribute) as String?) ?? ""
        let subrole = (attribute(element, kAXSubroleAttribute) as String?) ?? ""
        return role == secureMarker || subrole == secureMarker
    }

    private static func failure(_ requestId: String, _ code: ScreenTextErrorCode, _ message: String,
                                trusted: Bool) -> ScreenTextResponsePayload {
        ScreenTextResponsePayload(requestId: requestId, ok: false, error: message,
                                  errorCode: code, accessibilityTrusted: trusted)
    }

    // MARK: - AX helpers (as `ScreenContextReader`'s, which is private there)

    private static func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success,
              let value else { return nil }
        if T.self == AXUIElement.self {
            guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
            return (value as! T)
        }
        return value as? T
    }

    private static func children(_ element: AXUIElement) -> [AXUIElement]? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
              let array = value as? [AnyObject] else { return nil }
        return array.compactMap { item in
            CFGetTypeID(item) == AXUIElementGetTypeID() ? (item as! AXUIElement) : nil
        }
    }

    private static func nonEmpty(_ s: String?) -> String? {
        guard let s, !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return s
    }
}
