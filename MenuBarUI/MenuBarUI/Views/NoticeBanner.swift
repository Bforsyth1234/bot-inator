import AppKit
import SwiftUI

/// Dismissible banners for daemon `notice` frames (#19), e.g. "Full Disk
/// Access needed" with an "Open Settings" button. Renders nothing when
/// there are no active notices.
struct NoticeBanner: View {
    @EnvironmentObject private var ws: WebSocketManager

    var body: some View {
        if !ws.notices.isEmpty {
            // Capped so several notices can't push the popover's tabs and
            // button bar out of view; extra notices scroll.
            ScrollView {
                VStack(spacing: 6) {
                    ForEach(ws.notices) { notice in
                        NoticeRow(notice: notice) { ws.dismissNotice(id: notice.id) }
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
            }
            .frame(maxHeight: 200)
            .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct NoticeRow: View {
    let notice: NoticePayload
    let dismiss: () -> Void
    @State private var undoState: String?
    @State private var undoing = false

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 4) {
                Text(notice.title).appFont(.callout, weight: .semibold)
                Text(notice.body)
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
                    .lineLimit(4)
                if let url = settingsURL {
                    Button("Open Settings") { NSWorkspace.shared.open(url) }
                        .controlSize(.small)
                }
                if let entryId = undoEntryId {
                    if let undoState {
                        Text(undoState).appFont(.secondary).foregroundStyle(.secondary)
                    } else {
                        Button("Undo") { Task { await undo(entryId) } }
                            .controlSize(.small)
                            .disabled(undoing)
                    }
                }
            }
            Spacer(minLength: 0)
            Button(action: dismiss) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .help("Dismiss")
        }
        .padding(8)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    /// Only System Settings links are opened: the banner must not become a
    /// way to open arbitrary URLs.
    private var settingsURL: URL? {
        guard let action = notice.action, action.kind == "open_settings",
              let url = URL(string: action.url ?? ""),
              url.scheme == "x-apple.systempreferences" else { return nil }
        return url
    }

    /// #78: a standing rule's "Done automatically" notice carries the
    /// journal entry of the run; Undo is the user's click (no card needed).
    private var undoEntryId: String? {
        guard let action = notice.action, action.kind == "undo",
              let id = action.entryId, !id.isEmpty,
              id.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" })
        else { return nil }
        return id
    }

    @MainActor
    private func undo(_ entryId: String) async {
        undoing = true
        defer { undoing = false }
        let url = URL(string: "http://127.0.0.1:8000")!
            .appendingPathComponent("api/rules/runs/\(entryId)/undo")
        do {
            let request = try DaemonAuth.request(url, method: "POST")
            let (data, response) = try await URLSession.shared.data(for: request)
            try DaemonAuth.checkAuth(response)
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let status = (json?["status"] as? String) ?? ""
            let message = (json?["message"] as? String) ?? (json?["detail"] as? String)
            undoState = status == "undone" ? "Undone." : (message ?? "Not undone.")
        } catch {
            undoState = error.localizedDescription
        }
    }

    private var tint: Color {
        switch notice.level ?? .warning {
        case .info: return .blue
        case .warning: return .orange
        case .error: return .red
        }
    }

    private var icon: String {
        switch notice.level ?? .warning {
        case .info: return "info.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }
}
