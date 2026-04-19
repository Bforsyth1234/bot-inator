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

    func attach(to ws: WebSocketManager) {
        self.ws = ws
        ws.$pendingApproval
            .sink { [weak self] pending in
                self?.handle(pending)
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

    private func present(_ payload: ApprovalRequestPayload) {
        window?.close()

        let content = ApprovalAlert(payload: payload) { [weak self] approved, note in
            guard let self else { return }
            self.ws?.sendApprovalResponse(
                requestId: payload.requestId,
                approved: approved,
                userNote: note
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
        self.window = window

        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
    }

    private func closeWindow() {
        window?.close()
        window = nil
        presentedId = nil
    }
}
