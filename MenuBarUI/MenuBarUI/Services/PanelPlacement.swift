import AppKit

/// Places floating panels at the top-right of a screen and keeps them
/// fully on it (#101).
///
/// SwiftUI panels hosted with `sizingOptions = [.preferredContentSize]`
/// have no size until their content is laid out, so placing one with
/// `visibleFrame.maxX - panel.frame.width` put its left edge 16 pt from the
/// right edge (width 0) and the content then grew off screen. Here the
/// panel is sized from its content first, placed, and re-clamped whenever
/// it resizes (the Watch me steps list grows after Stop).
@MainActor
final class PanelPlacement {
    private var resizeObserver: NSObjectProtocol?

    deinit {
        if let resizeObserver { NotificationCenter.default.removeObserver(resizeObserver) }
    }

    /// Size `panel` from its content, then put its top-right corner `inset`
    /// from the right and `topInset` below the top of the screen's visible
    /// frame. `screen` defaults to the screen with the mouse.
    func placeTopRight(_ panel: NSWindow, inset: CGFloat = 16, topInset: CGFloat = 16,
                       screen: NSScreen? = nil) {
        Self.sizeToContent(panel)
        guard let visible = (screen ?? Self.screenWithMouse())?.visibleFrame else {
            panel.center()
            return
        }
        let size = panel.frame.size
        panel.setFrameTopLeftPoint(NSPoint(x: visible.maxX - size.width - inset,
                                           y: visible.maxY - topInset))
        Self.clamp(panel, to: visible)
        watchResizes(panel)
    }

    /// Keep `panel` inside its screen whenever its size changes.
    private func watchResizes(_ panel: NSWindow) {
        guard resizeObserver == nil else { return }
        resizeObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didResizeNotification, object: panel, queue: .main
        ) { [weak panel] _ in
            MainActor.assumeIsolated {
                guard let panel, let visible = (panel.screen ?? NSScreen.main)?.visibleFrame else { return }
                Self.clamp(panel, to: visible)
            }
        }
    }

    static func sizeToContent(_ panel: NSWindow) {
        guard let view = panel.contentViewController?.view ?? panel.contentView else { return }
        view.layoutSubtreeIfNeeded()
        let fitting = view.fittingSize
        if fitting.width > 0, fitting.height > 0 {
            panel.setContentSize(fitting)
        }
    }

    /// Move `panel` (never resize it) so its frame lies inside `visible`.
    /// A panel larger than the screen keeps its top-left corner on screen.
    static func clamp(_ panel: NSWindow, to visible: NSRect) {
        var origin = panel.frame.origin
        let size = panel.frame.size
        origin.x = min(origin.x, visible.maxX - size.width)
        origin.x = max(origin.x, visible.minX)
        origin.y = max(origin.y, visible.minY)
        origin.y = min(origin.y, visible.maxY - size.height)
        if origin != panel.frame.origin {
            panel.setFrameOrigin(origin)
        }
    }

    static func screenWithMouse() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
    }
}
