import AppKit
import Combine

/// The menu bar icon. Left-click opens the main window; right-click (or
/// Control-click) shows a short menu with Pause, Watch me, meeting notes and
/// Quit.
///
/// The icon is a pause glyph while paused, a record glyph while a "Watch me"
/// recording runs (#84), and shows the Inbox count (#87) beside it.
@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let ws: WebSocketManager
    private let demo: DemonstrationModel
    private let delivery: DeliveryViewModel
    private let openMain: (MainSection?) -> Void
    private let watchMe: () -> Void
    private var cancellables: Set<AnyCancellable> = []

    init(ws: WebSocketManager, demo: DemonstrationModel, delivery: DeliveryViewModel,
         openMain: @escaping (MainSection?) -> Void, watchMe: @escaping () -> Void) {
        self.ws = ws
        self.demo = demo
        self.delivery = delivery
        self.openMain = openMain
        self.watchMe = watchMe
        super.init()

        if let button = item.button {
            button.target = self
            button.action = #selector(clicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.imagePosition = .imageLeading
            button.toolTip = "bot-inator: click to open, right-click for more"
        }
        ws.$lastStatus
            .combineLatest(demo.$trace)
            .receive(on: RunLoop.main)
            .sink { [weak self] _, _ in self?.updateIcon() }
            .store(in: &cancellables)
        updateIcon()
    }

    private func updateIcon() {
        guard let button = item.button else { return }
        let glyph: String
        if ws.lastStatus?.paused == true {
            glyph = "pause.circle"
        } else if demo.isRecording {
            glyph = "record.circle.fill"
        } else {
            glyph = "brain.head.profile"
        }
        let image = NSImage(systemSymbolName: glyph, accessibilityDescription: "bot-inator")
        image?.isTemplate = true
        button.image = image
        let inbox = ws.lastStatus?.inboxCount ?? 0
        button.title = inbox > 0 ? "\(inbox)" : ""
    }

    @objc private func clicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        let wantsMenu = event?.type == .rightMouseUp
            || event?.modifierFlags.contains(.control) == true
        if wantsMenu {
            item.menu = makeMenu()
            item.button?.performClick(nil)
        } else {
            openMain(nil)
        }
    }

    /// Detach the menu once it closes so the next left-click opens the window.
    func menuDidClose(_ menu: NSMenu) {
        item.menu = nil
    }

    // MARK: Right-click menu

    private func makeMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self

        menu.addItem(action("Open bot-inator") { [openMain] in openMain(nil) })
        let inbox = ws.lastStatus?.inboxCount ?? 0
        menu.addItem(action(inbox > 0 ? "Inbox (\(inbox))" : "Inbox") { [openMain] in openMain(.inbox) })
        menu.addItem(action("Chat") { [openMain] in openMain(.chat) })
        menu.addItem(action("Settings…") { [openMain] in openMain(.settings) })
        menu.addItem(.separator())

        let delivery = self.delivery
        if delivery.isPaused {
            menu.addItem(action("Resume") { Task { await delivery.resume() } })
        } else {
            let pause = NSMenuItem(title: "Pause", action: nil, keyEquivalent: "")
            let sub = NSMenu()
            for duration in PauseDuration.allCases {
                sub.addItem(action(duration.label) { Task { await delivery.pause(duration) } })
            }
            pause.submenu = sub
            menu.addItem(pause)
        }
        menu.addItem(action(demo.isRecording ? "Stop Watch me" : "Watch me…") { [watchMe] in watchMe() })
        let recorder = MeetingRecorder.shared
        if recorder.isRecording {
            menu.addItem(action("Stop taking notes") { Task { await recorder.stopFromClick() } })
        } else {
            menu.addItem(action("Take meeting notes now") {
                let consent = MeetingRecordingConsent.userClicked()
                Task { await recorder.startManual(consent: consent) }
            })
        }
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit bot-inator",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))
        return menu
    }

    private func action(_ title: String, _ handler: @escaping () -> Void) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: #selector(ClosureTarget.fire), keyEquivalent: "")
        let target = ClosureTarget(handler)
        menuItem.target = target
        menuItem.representedObject = target // keeps the target alive
        return menuItem
    }
}

/// An NSMenuItem target that runs a closure.
private final class ClosureTarget: NSObject {
    private let handler: () -> Void
    init(_ handler: @escaping () -> Void) { self.handler = handler }
    @objc func fire() { handler() }
}
