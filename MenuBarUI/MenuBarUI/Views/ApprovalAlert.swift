import SwiftUI

/// Sheet-style approval dialog shown when an `approval_request` message arrives.
struct ApprovalAlert: View {
    let payload: ApprovalRequestPayload
    /// Invoked on Approve/Deny. `editedArgs` is non-nil only when the user
    /// modified the tool's arguments in a specialized editor (e.g. the
    /// iMessage body); the backend substitutes these values in place of the
    /// agent's original proposal.
    let onDecision: (_ approved: Bool, _ note: String, _ editedArgs: JSONValue?) -> Void

    @State private var userNote: String = ""
    @State private var draftTarget: String = ""
    @State private var draftMessage: String = ""
    @Environment(\.dismiss) private var dismiss

    private var isIMessage: Bool { payload.toolName == "send_imessage" }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            reasoningSection
            if isIMessage {
                imessageEditor
            } else {
                argsSection
            }
            noteSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 480, height: isIMessage ? 520 : 460)
        .onAppear(perform: populateDrafts)
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

    private var imessageEditor: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text("To")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                TextField("+14155551212 or name@icloud.com", text: $draftTarget)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.body, design: .monospaced))
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Message")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                TextEditor(text: $draftMessage)
                    .font(.body)
                    .frame(minHeight: 120)
                    .padding(6)
                    .background(Color(nsColor: .textBackgroundColor),
                                in: RoundedRectangle(cornerRadius: 6))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .stroke(Color.secondary.opacity(0.25))
                    )
            }
        }
    }

    private var buttonRow: some View {
        HStack {
            Spacer()
            Button(role: .destructive) {
                onDecision(false, userNote, nil)
                dismiss()
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                onDecision(true, userNote, buildEditedArgs())
                dismiss()
            } label: {
                Label(isIMessage ? "Approve & Send" : "Approve",
                      systemImage: "checkmark.circle.fill")
            }
            .keyboardShortcut(.defaultAction)
            .buttonStyle(.borderedProminent)
            .disabled(isIMessage && draftMessage.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty)
        }
    }

    // MARK: - iMessage helpers

    private func populateDrafts() {
        guard isIMessage else { return }
        let kwargs = payload.toolArgs.kwargs
        if let target = kwargs["target_number"]?.stringValue, draftTarget.isEmpty {
            draftTarget = target
        }
        if let message = kwargs["message"]?.stringValue, draftMessage.isEmpty {
            draftMessage = message
        }
    }

    /// Build an ``edited_args`` payload when the user touched the editable
    /// fields. Returns `nil` for tools without a specialized editor.
    private func buildEditedArgs() -> JSONValue? {
        guard isIMessage else { return nil }
        return .object([
            "args": .array([]),
            "kwargs": .object([
                "target_number": .string(draftTarget),
                "message": .string(draftMessage),
            ]),
        ])
    }
}

// MARK: - JSONValue conveniences

private extension JSONValue {
    var kwargs: [String: JSONValue] {
        if case .object(let dict) = self,
           case .object(let kw)? = dict["kwargs"] {
            return kw
        }
        return [:]
    }

    var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }
}
