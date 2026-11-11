import AppKit
import Carbon.HIToolbox
import Combine

/// The quick-ask shortcut choices (#74). A short fixed list keeps clear of
/// system shortcuts (⌘Space is Spotlight, ⌃Space is the input-source switch)
/// without a key recorder. Stored in `UserDefaults` under `storageKey`.
enum QuickAskShortcut: String, CaseIterable, Identifiable {
    case optionSpace
    case optionCommandSpace
    case controlOptionSpace
    case shiftCommandSpace
    case off

    static let storageKey = "quickAskShortcut"
    static let fallback: QuickAskShortcut = .optionSpace

    var id: String { rawValue }

    static var current: QuickAskShortcut {
        let raw = UserDefaults.standard.string(forKey: storageKey) ?? fallback.rawValue
        return QuickAskShortcut(rawValue: raw) ?? fallback
    }

    var label: String {
        switch self {
        case .optionSpace: return "⌥Space"
        case .optionCommandSpace: return "⌥⌘Space"
        case .controlOptionSpace: return "⌃⌥Space"
        case .shiftCommandSpace: return "⇧⌘Space"
        case .off: return "Off"
        }
    }

    /// Carbon virtual key code + modifier mask; nil for `.off`.
    var carbon: (keyCode: UInt32, modifiers: UInt32)? {
        let space = UInt32(kVK_Space)
        switch self {
        case .optionSpace: return (space, UInt32(optionKey))
        case .optionCommandSpace: return (space, UInt32(optionKey | cmdKey))
        case .controlOptionSpace: return (space, UInt32(controlKey | optionKey))
        case .shiftCommandSpace: return (space, UInt32(shiftKey | cmdKey))
        case .off: return nil
        }
    }
}

/// One global hotkey via Carbon `RegisterEventHotKey` (no Accessibility
/// permission needed). `register` replaces the previous registration, so
/// changing the shortcut is one call.
@MainActor
final class HotKey {
    /// Why the last `register` failed (e.g. another app owns the shortcut);
    /// nil when it succeeded or nothing is registered.
    private(set) var lastError: String?
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private var action: (() -> Void)?
    private static let signature: OSType = 0x4249_4E51  // "BINQ"
    private static let hotKeyId: UInt32 = 1

    init() {}

    /// Register `keyCode` + `modifiers` (Carbon masks). Returns false and
    /// sets `lastError` when macOS refuses (the shortcut is taken).
    @discardableResult
    func register(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) -> Bool {
        unregister()
        installHandlerIfNeeded()
        self.action = action
        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: Self.signature, id: Self.hotKeyId)
        let status = RegisterEventHotKey(keyCode, modifiers, id,
                                         GetApplicationEventTarget(), 0, &ref)
        guard status == noErr, let ref else {
            lastError = status == eventHotKeyExistsErr
                ? "Another app already uses this shortcut."
                : "macOS refused the shortcut (error \(status))."
            self.action = nil
            return false
        }
        hotKeyRef = ref
        lastError = nil
        return true
    }

    func unregister() {
        if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
        hotKeyRef = nil
        action = nil
        lastError = nil
    }

    fileprivate func fire() { action?() }

    private func installHandlerIfNeeded() {
        guard handlerRef == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, event, userData in
            guard let event, let userData else { return OSStatus(eventNotHandledErr) }
            var id = EventHotKeyID()
            let err = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                        EventParamType(typeEventHotKeyID), nil,
                                        MemoryLayout<EventHotKeyID>.size, nil, &id)
            guard err == noErr, id.signature == HotKey.signature else {
                return OSStatus(eventNotHandledErr)
            }
            let hotKey = Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue()
            // Carbon delivers application-target events on the main thread.
            MainActor.assumeIsolated { hotKey.fire() }
            return noErr
        }, 1, &spec, me, &handlerRef)
    }
}

/// Keeps the quick-ask hotkey registered to the stored `QuickAskShortcut`,
/// re-registering when Settings changes it. One per app (`shared`), so the
/// Settings row can show a registration error.
@MainActor
final class QuickAskHotKeyController: ObservableObject {
    static let shared = QuickAskHotKeyController()

    @Published private(set) var registeredShortcut: QuickAskShortcut = .off
    @Published private(set) var lastError: String?
    private let hotKey = HotKey()
    private var action: () -> Void = {}
    private var defaultsObserver: NSObjectProtocol?

    private init() {}

    /// Register the stored shortcut to run `action`, and follow changes.
    func start(action: @escaping () -> Void) {
        self.action = action
        apply(QuickAskShortcut.current)
        guard defaultsObserver == nil else { return }
        defaultsObserver = NotificationCenter.default.addObserver(
            forName: UserDefaults.didChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                let wanted = QuickAskShortcut.current
                if wanted != self.registeredShortcut { self.apply(wanted) }
            }
        }
    }

    /// Try the stored shortcut again (Settings "Retry" after a conflict).
    func retry() { apply(QuickAskShortcut.current) }

    private func apply(_ shortcut: QuickAskShortcut) {
        guard let carbon = shortcut.carbon else {
            hotKey.unregister()
            registeredShortcut = .off
            lastError = nil
            return
        }
        let ok = hotKey.register(keyCode: carbon.keyCode, modifiers: carbon.modifiers) { [weak self] in
            self?.action()
        }
        registeredShortcut = shortcut
        lastError = ok ? nil : hotKey.lastError
    }
}
