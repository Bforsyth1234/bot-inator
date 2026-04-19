import SwiftUI

/// Floating-window dialog shown when a `code_approval_request` message
/// arrives. Presents the AI-drafted Python source in a monospaced editor
/// so the user can inspect — and optionally edit — the code before it is
/// persisted into `tools/generated/` and hot-loaded by the daemon.
struct CodeReviewAlert: View {
    let payload: CodeApprovalRequestPayload
    /// Invoked on Approve/Deny. `editedCode` is non-nil only when the user
    /// modified the draft in the editor; the backend writes that text to
    /// disk in place of the original.
    let onDecision: (_ approved: Bool, _ editedCode: String?, _ note: String) -> Void

    @State private var draft: String = ""
    @State private var userNote: String = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            descriptionSection
            codeEditorSection
            noteSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 620, height: 560)
        .onAppear { if draft.isEmpty { draft = payload.code } }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "wand.and.stars")
                .foregroundStyle(.purple)
                .font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text("Code Review")
                    .font(.headline)
                Text(payload.toolName)
                    .font(.subheadline.monospaced())
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let timeout = payload.timeoutSeconds {
                Text("\(timeout)s")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var descriptionSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Description")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(payload.description)
                .font(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var codeEditorSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Generated source")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            TextEditor(text: $draft)
                .font(.system(.caption, design: .monospaced))
                .frame(minHeight: 300)
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
                .font(.caption.weight(.semibold))
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
                dismiss()
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                let edited: String? =
                    (draft != payload.code) ? draft : nil
                onDecision(true, edited, userNote)
                dismiss()
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
