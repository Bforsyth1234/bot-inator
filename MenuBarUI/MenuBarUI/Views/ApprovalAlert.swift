import SwiftUI

/// Sheet-style approval dialog shown when an `approval_request` message arrives.
struct ApprovalAlert: View {
    let payload: ApprovalRequestPayload
    let onDecision: (_ approved: Bool, _ note: String) -> Void

    @State private var userNote: String = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            reasoningSection
            argsSection
            noteSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 480, height: 460)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.shield.fill")
                .foregroundStyle(.orange)
                .font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text("Approval Required")
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

    private var reasoningSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Reasoning")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(payload.reasoning)
                .font(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var argsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Arguments")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ScrollView {
                Text(payload.toolArgs.prettyPrinted())
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            .frame(maxHeight: 140)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25))
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
                onDecision(false, userNote)
                dismiss()
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                onDecision(true, userNote)
                dismiss()
            } label: {
                Label("Approve", systemImage: "checkmark.circle.fill")
            }
            .keyboardShortcut(.defaultAction)
            .buttonStyle(.borderedProminent)
        }
    }
}
