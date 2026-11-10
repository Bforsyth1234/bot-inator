import SwiftUI

/// Floating-window dialog shown when a `code_approval_request` message
/// arrives. Presents the AI-drafted Python source in a monospaced editor
/// so the user can inspect — and optionally edit — the code before it is
/// persisted into `tools/generated/` and hot-loaded by the daemon.
struct CodeReviewAlert: View {
    let payload: CodeApprovalRequestPayload
    /// Invoked on Approve/Deny. `editedCode` is non-nil only when the user
    /// modified the draft in the editor; the backend writes that text to
    /// disk in place of the original. The presenter closes the window; its
    /// red X sends a denial (#15).
    let onDecision: (_ approved: Bool, _ editedCode: String?, _ note: String) -> Void

    @State private var draft: String = ""
    @State private var userNote: String = ""
    @Environment(\.textSize) private var textSize

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            if let summary { summarySection(summary) }
            descriptionSection
            codeEditorSection
            noteSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 680,
               height: min(740, ((summary != nil ? 700 : 600) * textSize.layoutScale).rounded()))
        .onAppear { if draft.isEmpty { draft = payload.code } }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "wand.and.stars")
                .foregroundStyle(.purple)
                .appFont(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text("Code Review")
                    .appFont(.headline)
                Text(payload.toolName)
                    .appFont(.label, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let timeout = payload.timeoutSeconds {
                Text("\(timeout)s")
                    .appFont(.secondary, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// Plain-language summary from the daemon (#64); nil from an older
    /// daemon, which keeps the previous layout.
    private var summary: String? {
        guard let text = payload.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    private func summarySection(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("What this tool does")
                .appFont(.secondary, weight: .semibold)
                .foregroundStyle(.secondary)
            Text(text)
                .appFont(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var descriptionSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Description")
                .appFont(.secondary, weight: .semibold)
                .foregroundStyle(.secondary)
            Text(payload.description)
                .appFont(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var codeEditorSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Generated source")
                .appFont(.secondary, weight: .semibold)
                .foregroundStyle(.secondary)
            TextEditor(text: $draft)
                .appFont(.secondary, design: .monospaced)
                .frame(minHeight: summary != nil ? 200 : 260)
                .padding(6)
                .background(Color(nsColor: .textBackgroundColor),
                            in: RoundedRectangle(cornerRadius: 6))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.secondary.opacity(0.25))
                )
        }
    }

    private var noteSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Note (optional)")
                .appFont(.secondary, weight: .semibold)
                .foregroundStyle(.secondary)
            TextField("Add a note sent with your decision", text: $userNote)
                .textFieldStyle(.roundedBorder)
        }
    }

    private var buttonRow: some View {
        HStack {
            Spacer()
            Button(role: .destructive) {
                onDecision(false, nil, userNote)
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                let edited: String? =
                    (draft != payload.code) ? draft : nil
                onDecision(true, edited, userNote)
            } label: {
                Label("Approve & Install", systemImage: "checkmark.circle.fill")
            }
            .keyboardShortcut(.defaultAction)
            .buttonStyle(.borderedProminent)
            .disabled(draft.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty)
        }
    }
}
