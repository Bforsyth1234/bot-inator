import AppKit
import Combine
import SwiftUI

/// Presents `ApprovalAlert` in a standalone floating window whenever a new
/// approval request arrives on the WebSocket. Lives for the full app lifetime
/// so approvals surface even while the menu-bar popover is closed.
@MainActor
final class ApprovalPresenter {
    private var cancellables = Set<AnyCancellable>()
    private weak var ws: WebSocketManager?
    private var window: NSWindow?
    private var presentedId: String?
    private var codeWindow: NSWindow?
    private var presentedCodeId: String?
    /// NSWindow.delegate is weak; these keep the close handlers alive.
    private var windowDelegate: WindowCloseHandler?
    private var codeWindowDelegate: WindowCloseHandler?

    func attach(to ws: WebSocketManager) {
        self.ws = ws
        ws.$pendingApproval
            .sink { [weak self] pending in
                self?.handle(pending)
            }
            .store(in: &cancellables)
        ws.$pendingCodeApproval
            .sink { [weak self] pending in
                self?.handleCode(pending)
            }
            .store(in: &cancellables)
    }

    private func handle(_ pending: (seq: Int, payload: ApprovalRequestPayload)?) {
        guard let pending else {
            closeWindow()
            return
        }
        if presentedId == pending.payload.requestId, window != nil { return }
        presentedId = pending.payload.requestId
        present(pending.payload)
    }

    private func handleCode(
        _ pending: (seq: Int, payload: CodeApprovalRequestPayload)?
    ) {
        guard let pending else {
            closeCodeWindow()
            return
        }
        if presentedCodeId == pending.payload.requestId, codeWindow != nil { return }
        presentedCodeId = pending.payload.requestId
        presentCode(pending.payload)
    }

    private func present(_ payload: ApprovalRequestPayload) {
        window?.close()

        let content = ApprovalAlert(payload: payload) { [weak self] approved, note, editedArgs in
            guard let self, self.presentedId == payload.requestId else { return }
            self.ws?.sendApprovalResponse(
                requestId: payload.requestId,
                approved: approved,
                userNote: note,
                editedArgs: editedArgs
            )
            self.closeWindow()
        }

        let hosting = NSHostingController(rootView: content)
        let window = NSWindow(contentViewController: hosting)
        window.title = "Approval Required"
        window.styleMask = [.titled, .closable]
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.center()
        // #15: the red X (or Cmd-W) is a Deny. AppKit only asks the
        // delegate on a user close, never on our programmatic close() after
        // Approve/Deny, and the presentedId check makes it fire once.
        let delegate = WindowCloseHandler { [weak self] in
            self?.userClosedApproval(requestId: payload.requestId)
        }
        window.delegate = delegate
        self.windowDelegate = delegate
        self.window = window

        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
    }

    private func presentCode(_ payload: CodeApprovalRequestPayload) {
        codeWindow?.close()

        let content = CodeReviewAlert(payload: payload) { [weak self] approved, editedCode, note in
            guard let self, self.presentedCodeId == payload.requestId else { return }
            self.ws?.sendCodeApprovalResponse(
                requestId: payload.requestId,
                approved: approved,
                editedCode: editedCode,
                userNote: note
            )
            self.closeCodeWindow()
        }

        let hosting = NSHostingController(rootView: content)
        let window = NSWindow(contentViewController: hosting)
        window.title = "Review Generated Tool"
        window.styleMask = [.titled, .closable, .resizable]
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.center()
        let delegate = WindowCloseHandler { [weak self] in
            self?.userClosedCodeReview(requestId: payload.requestId)
        }
        window.delegate = delegate
        self.codeWindowDelegate = delegate
        self.codeWindow = window

        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
    }

    /// The user closed the approval window without answering: send one
    /// Deny. State is cleared first, so the WebSocket's `pendingApproval =
    /// nil` re-entering `closeWindow()` finds nothing to close and a second
    /// close (or a late button action) finds no presented request.
    private func userClosedApproval(requestId: String) {
        guard presentedId == requestId else { return }
        // Keep the closing window and its delegate alive until AppKit is
        // done with this close.
        let closing = (window, windowDelegate)
        DispatchQueue.main.async { _ = closing }
        window = nil
        windowDelegate = nil
        presentedId = nil
        ws?.sendApprovalResponse(
            requestId: requestId, approved: false, userNote: nil, editedArgs: nil
        )
    }

    private func userClosedCodeReview(requestId: String) {
        guard presentedCodeId == requestId else { return }
        // Keep the closing window and its delegate alive until AppKit is
        // done with this close.
        let closing = (codeWindow, codeWindowDelegate)
        DispatchQueue.main.async { _ = closing }
        codeWindow = nil
        codeWindowDelegate = nil
        presentedCodeId = nil
        ws?.sendCodeApprovalResponse(
            requestId: requestId, approved: false, editedCode: nil, userNote: nil
        )
    }

    private func closeWindow() {
        window?.delegate = nil
        window?.close()
        window = nil
        windowDelegate = nil
        presentedId = nil
    }

    private func closeCodeWindow() {
        codeWindow?.delegate = nil
        codeWindow?.close()
        codeWindow = nil
        codeWindowDelegate = nil
        presentedCodeId = nil
    }
}

/// Window delegate that reports a user-initiated close (red X / Cmd-W) and
/// lets it proceed.
private final class WindowCloseHandler: NSObject, NSWindowDelegate {
    private let onUserClose: () -> Void

    init(onUserClose: @escaping () -> Void) {
        self.onUserClose = onUserClose
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        onUserClose()
        return true
    }
}
