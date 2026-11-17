import AppKit
import SwiftUI

/// Which sidebar page the main window shows. Shared so the menu bar icon,
/// the menu commands and the Dock can open the window on a given page.
@MainActor
final class MainWindowRouter: ObservableObject {
    private static let storageKey = "mainWindow.section"

    @Published var section: MainSection {
        didSet { UserDefaults.standard.set(section.rawValue, forKey: Self.storageKey) }
    }

    init() {
        let saved = UserDefaults.standard.string(forKey: Self.storageKey)
        section = saved.flatMap(MainSection.init(rawValue:)) ?? .thoughts
    }
}

/// Owns the main app window. It is an AppKit window (not a SwiftUI scene) so
/// the menu bar icon, the Dock and launch can all open it directly; closing
/// it only hides it, and the next ``show()`` brings back the same window.
@MainActor
final class MainWindowController {
    private var window: NSWindow?
    private let makeContent: () -> AnyView

    init(makeContent: @escaping () -> AnyView) {
        self.makeContent = makeContent
    }

    func show() {
        let window = self.window ?? makeWindow()
        self.window = window
        // Dock + Cmd-Tab while the window is open (AppDelegate switches back
        // to menu-bar-only when the last window closes).
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        // macOS 14 may decline activation; still put the window on top.
        window.orderFrontRegardless()
    }

    private func makeWindow() -> NSWindow {
        let hosting = NSHostingController(rootView: makeContent())
        // Let the SwiftUI toolbar and navigation title reach the NSWindow.
        hosting.sceneBridgingOptions = [.toolbars, .title]
        // The window's size is ours (and the user's), not the view's ideal size.
        hosting.sizingOptions = []
        let window = NSWindow(contentViewController: hosting)
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window.toolbarStyle = .unified
        window.title = "bot-inator"
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.setContentSize(NSSize(width: 980, height: 700))
        window.contentMinSize = NSSize(width: 760, height: 520)
        if !window.setFrameUsingName("MainWindow") {
            window.center()
        }
        window.setFrameAutosaveName("MainWindow")
        return window
    }
}
