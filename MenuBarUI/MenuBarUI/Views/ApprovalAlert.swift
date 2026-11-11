import SwiftUI

/// Sheet-style approval dialog shown when an `approval_request` message arrives.
struct ApprovalAlert: View {
    let payload: ApprovalRequestPayload
    /// Invoked on Approve/Deny. `editedArgs` is non-nil only when the user
    /// changed a field in the tool's editable card; the backend substitutes
    /// these values in place of the agent's original proposal.
    let onDecision: (_ approved: Bool, _ note: String, _ editedArgs: JSONValue?) -> Void

    @State private var userNote: String = ""
    @State private var values: [String: String] = [:]
    @State private var populated = false
    @State private var showTechnical = false
    /// Bundled card (#76): one tick and one set of field values per action.
    @State private var ticks: [Bool] = []
    @State private var actionValues: [[String: String]] = []
    @Environment(\.textSize) private var textSize

    /// The plain-language summary of a suggested tool (#64), when the
    /// daemon sent one. Nil (older daemon, other tools) keeps the old layout.
    private var toolIdeaSummary: String? {
        guard payload.toolName == "generate_custom_tool",
              let text = payload.summary?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    /// A prepared offer with two or more actions (#76), else nil. A
    /// one-action offer uses that tool's own editable card.
    private var bundleActions: [ApprovalAction]? {
        guard let actions = payload.actions, actions.count >= 2 else { return nil }
        return actions
    }

    /// Fixed window height: grows with the Text size setting, capped so the
    /// buttons stay on a 13" screen (the card scrolls past that).
    private var windowHeight: CGFloat {
        let base: CGFloat = toolIdeaSummary != nil ? 580
            : bundleActions.map(BundleCard.windowHeight) ?? (spec?.windowHeight ?? 480)
        return min(ApprovalEditSpec.maxWindowHeight, (base * textSize.layoutScale).rounded())
    }

    /// The editable card for this tool, or nil for the read-only JSON view.
    private var spec: ApprovalEditSpec? {
        ApprovalEditSpec.spec(for: payload.toolName, toolArgs: payload.toolArgs)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            // #62: the card scrolls between the header and the buttons, so
            // larger text can never push Approve/Deny out of the window.
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    originNotesSection
                    if let summary = toolIdeaSummary {
                        toolIdeaCard(summary)
                    } else if let actions = bundleActions {
                        reasoningSection
                        bundleCard(actions)
                    } else {
                        reasoningSection
                        if let spec {
                            card(spec)
                        } else {
                            argsSection
                        }
                    }
                    noteSection
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                // Room for focus rings, which a scroll view would clip.
                .padding(3)
            }
            .padding(-3)
            .frame(maxHeight: .infinity)
            buttonRow
        }
        .padding(18)
        .frame(width: 520, height: windowHeight)
        .onAppear(perform: populate)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: toolIdeaSummary != nil
                  ? "lightbulb.fill" : "exclamationmark.shield.fill")
                .foregroundStyle(.orange)
                .appFont(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(toolIdeaSummary != nil ? "New Tool Idea"
                     : bundleActions != nil ? "Ready When You Are" : "Approval Required")
                    .appFont(.headline)
                Text(payload.toolName)
                    .appFont(.label, design: .monospaced)
                    .foregroundStyle(.secondary)
                if payload.outsideSandbox == true {
                    // #104: the user trusted this generated tool's code.
                    Label("Runs outside the sandbox", systemImage: "lock.open.fill")
                        .appFont(.secondary, weight: .semibold)
                        .foregroundStyle(.orange)
                }
            }
            Spacer()
            if let timeout = payload.timeoutSeconds {
                Text("\(timeout)s")
                    .appFont(.secondary, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// #81: this turn read outside text (an email, a page, a message), so
    /// say where the card's targets came from. Empty on unmarked turns.
    @ViewBuilder
    private var originNotesSection: some View {
        if let notes = payload.originNotes, !notes.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Label("Shaped by text it read", systemImage: "exclamationmark.triangle.fill")
                    .appFont(.label)
                    .foregroundStyle(.orange)
                ForEach(Array(notes.enumerated()), id: \.offset) { _, note in
                    Text(note)
                        .appFont(.secondary)
                        .foregroundStyle(.orange)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var reasoningSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            caption("Reasoning")
            Text(payload.reasoning)
                .appFont(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var argsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            caption("Arguments")
            ScrollView {
                Text(payload.toolArgs.withoutEmptyArgs().prettyPrinted())
                    .appFont(.secondary, design: .monospaced)
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

    // MARK: - Suggested-tool card (#64)

    /// Leads with the plain-language summary; the raw call (name,
    /// description, expected logic) stays available under a disclosure.
    private func toolIdeaCard(_ summary: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                caption("What this tool would do")
                Text(summary)
                    .appFont(.body)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Approving lets the assistant draft the code. You review that code before it is installed.")
                .appFont(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            DisclosureGroup(isExpanded: $showTechnical) {
                argsSection.padding(.top, 4)
            } label: {
                caption("Technical details")
            }
        }
    }

    // MARK: - Bundled card (#76)

    /// One tick box per action; a ticked action shows its editable fields.
    /// Unticked actions go back with `enabled: false` and do not run.
    private func bundleCard(_ actions: [ApprovalAction]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            caption("Approving does the ticked steps")
            ForEach(Array(actions.enumerated()), id: \.offset) { index, action in
                VStack(alignment: .leading, spacing: 8) {
                    Toggle(isOn: tickBinding(index)) {
                        Text(BundleCard.title(action))
                            .appFont(.body, weight: .semibold)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .toggleStyle(.checkbox)
                    if let spec = BundleCard.spec(action), index < ticks.count, ticks[index] {
                        actionFields(spec, action: action, index: index)
                            .padding(.leading, 22)
                    }
                }
                .padding(10)
                .background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }

    private func actionFields(_ spec: ApprovalEditSpec, action: ApprovalAction, index: Int) -> some View {
        let toolArgs = BundleCard.toolArgs(action)
        let merged = ApprovalEditSpec.mergedKwargs(toolArgs, params: spec.params)
        return VStack(alignment: .leading, spacing: 8) {
            ForEach(spec.readOnly(merged), id: \.label) { row in
                VStack(alignment: .leading, spacing: 2) {
                    caption(row.label)
                    Text(row.value).appFont(.body).textSelection(.enabled)
                }
            }
            ForEach(spec.fields, id: \.key) { field in
                VStack(alignment: .leading, spacing: 4) {
                    caption(field.required ? field.label : "\(field.label) (optional)")
                    TextField(field.placeholder, text: actionBinding(index, field.key),
                              axis: field.multiline ? .vertical : .horizontal)
                        .textFieldStyle(.roundedBorder)
                        .lineLimit(field.multiline ? 2...4 : 1...1)
                        .appFont(.body, design: field.monospaced ? .monospaced : .default)
                }
            }
        }
    }

    private func tickBinding(_ index: Int) -> Binding<Bool> {
        Binding(get: { index < ticks.count ? ticks[index] : true },
                set: { if index < ticks.count { ticks[index] = $0 } })
    }

    private func actionBinding(_ index: Int, _ key: String) -> Binding<String> {
        Binding(get: { index < actionValues.count ? actionValues[index][key] ?? "" : "" },
                set: { if index < actionValues.count { actionValues[index][key] = $0 } })
    }

    private var noteSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            caption("Note (optional)")
            TextField("Add a note sent with your decision", text: $userNote)
                .textFieldStyle(.roundedBorder)
        }
    }

    // MARK: - Editable card

    private func card(_ spec: ApprovalEditSpec) -> some View {
        let merged = ApprovalEditSpec.mergedKwargs(payload.toolArgs, params: spec.params)
        return VStack(alignment: .leading, spacing: 10) {
            ForEach(spec.readOnly(merged), id: \.label) { row in
                VStack(alignment: .leading, spacing: 2) {
                    caption(row.label)
                    Text(row.value)
                        .appFont(.body)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(spec.fields, id: \.key) { field in
                VStack(alignment: .leading, spacing: 4) {
                    caption(field.required ? field.label : "\(field.label) (optional)")
                    if field.multiline {
                        TextEditor(text: binding(field.key))
                            .appFont(.body)
                            .frame(minHeight: 80)
                            .padding(6)
                            .background(Color(nsColor: .textBackgroundColor),
                                        in: RoundedRectangle(cornerRadius: 6))
                            .overlay(
                                RoundedRectangle(cornerRadius: 6)
                                    .stroke(Color.secondary.opacity(0.25))
                            )
                    } else {
                        TextField(field.placeholder, text: binding(field.key))
                            .textFieldStyle(.roundedBorder)
                            .appFont(.body, design: field.monospaced ? .monospaced : .default)
                    }
                }
            }
        }
    }

    private func binding(_ key: String) -> Binding<String> {
        Binding(get: { values[key] ?? "" }, set: { values[key] = $0 })
    }

    private func caption(_ text: String) -> some View {
        Text(text)
            .appFont(.secondary, weight: .semibold)
            .foregroundStyle(.secondary)
    }

    private var buttonRow: some View {
        HStack {
            Spacer()
            Button(role: .destructive) {
                onDecision(false, userNote, nil)
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                if let actions = bundleActions {
                    onDecision(true, userNote,
                               BundleCard.editedArgs(actions, ticks: ticks, values: actionValues))
                    return
                }
                let edited = spec.flatMap {
                    ApprovalEditSpec.editedArgs($0, toolArgs: payload.toolArgs, values: values)
                }
                onDecision(true, userNote, edited)
            } label: {
                Label(approveLabel, systemImage: "checkmark.circle.fill")
            }
            .keyboardShortcut(.defaultAction)
            .buttonStyle(.borderedProminent)
            .disabled(!approveEnabled)
        }
    }

    private var approveLabel: String {
        if bundleActions != nil {
            let count = ticks.filter { $0 }.count
            return count == 1 ? "Approve 1 Step" : "Approve \(count) Steps"
        }
        return spec?.approveLabel ?? "Approve"
    }

    private var approveEnabled: Bool {
        if let actions = bundleActions {
            return BundleCard.isComplete(actions, ticks: ticks, values: actionValues)
        }
        return spec?.isComplete(values) ?? true
    }

    private func populate() {
        guard !populated else { return }
        populated = true
        if let actions = bundleActions {
            ticks = actions.map { $0.enabled ?? true }
            actionValues = actions.map { action in
                BundleCard.spec(action).map {
                    ApprovalEditSpec.initialValues($0, toolArgs: BundleCard.toolArgs(action))
                } ?? [:]
            }
            return
        }
        guard let spec else { return }
        values = ApprovalEditSpec.initialValues(spec, toolArgs: payload.toolArgs)
    }
}

// MARK: - Light card (#83)

/// The small card for a call the daemon marked `reversible` (the action
/// journal can undo it). Still a card (invariant 7a): nothing runs until the
/// user approves. Return approves, Escape denies, "Edit…" opens the full
/// card with every field. The presenter shows it in a panel that does not
/// activate the app, so the app the user is typing in keeps focus until the
/// user clicks the panel.
struct CompactApprovalCard: View {
    let payload: ApprovalRequestPayload
    let onDecision: (_ approved: Bool) -> Void
    let onEdit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "arrow.uturn.backward.circle.fill")
                    .foregroundStyle(.orange)
                    .appFont(.headline)
                Text(CompactCard.title(payload))
                    .appFont(.headline)
                    .lineLimit(1)
                Spacer()
                if let timeout = payload.timeoutSeconds {
                    Text("\(timeout)s")
                        .appFont(.secondary, design: .monospaced)
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(CompactCard.lines(payload), id: \.label) { row in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(row.label)
                        .appFont(.secondary, weight: .semibold)
                        .foregroundStyle(.secondary)
                    Text(row.value)
                        .appFont(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                }
            }
            Text("You can undo this later in History.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
            HStack {
                Button("Edit…", action: onEdit)
                    .buttonStyle(.borderless)
                Spacer()
                Button(role: .destructive) {
                    onDecision(false)
                } label: {
                    Text("Deny")
                }
                .keyboardShortcut(.cancelAction)
                Button {
                    onDecision(true)
                } label: {
                    Text(CompactCard.approveLabel(payload))
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(!CompactCard.isComplete(payload))
            }
        }
        .padding(14)
        .frame(width: 380)
    }
}

/// Display-only clean-up of a card's arguments (#111): an argument the
/// model left empty (`null`, `""`, `[]`, `{}`) is not shown, as on the
/// phone (`card_text`). `false` and `0` are values and stay. Positional
/// args keep their places (removing one would renumber the rest); an empty
/// `args` list is dropped. The payload sent back on approval is unchanged.
extension JSONValue {
    var isEmptyArg: Bool {
        switch self {
        case .null: return true
        case .string(let v): return v.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .array(let v): return v.isEmpty
        case .object(let v): return v.isEmpty
        default: return false
        }
    }

    func withoutEmptyArgs() -> JSONValue {
        guard case .object(var top) = self else { return self }
        if case .object(let kwargs)? = top["kwargs"] {
            top["kwargs"] = .object(kwargs.filter { !$0.value.isEmptyArg })
        } else if top["args"] == nil {
            top = top.filter { !$0.value.isEmptyArg }
        }
        if top["args"]?.isEmptyArg == true { top["args"] = nil }
        return .object(top)
    }
}

/// Pure logic for the light card (#83).
enum CompactCard {
    /// A reversible one-call card (not a #76 bundle, not an idea card).
    static func applies(_ payload: ApprovalRequestPayload) -> Bool {
        payload.reversible == true && (payload.actions?.isEmpty ?? true)
            && payload.toolName != "generate_custom_tool"
    }

    static func title(_ payload: ApprovalRequestPayload) -> String {
        switch payload.toolName {
        case "move_file": return "Move a file?"
        case "create_event": return "Add a calendar event?"
        case "create_reminder": return "Add a reminder?"
        case "create_note": return "Create a note?"
        default: return "Run \(payload.toolName)?"
        }
    }

    static func approveLabel(_ payload: ApprovalRequestPayload) -> String {
        ApprovalEditSpec.spec(for: payload.toolName, toolArgs: payload.toolArgs)?.approveLabel
            ?? "Approve"
    }

    /// The card's read-only rows, then every non-empty field, at most four.
    static func lines(_ payload: ApprovalRequestPayload) -> [ApprovalInfoRow] {
        guard let spec = ApprovalEditSpec.spec(for: payload.toolName, toolArgs: payload.toolArgs) else {
            return [ApprovalInfoRow(label: "Arguments",
                                    value: payload.toolArgs.withoutEmptyArgs().prettyPrinted()
                                        .split(whereSeparator: \.isNewline).joined(separator: " "))]
        }
        let merged = ApprovalEditSpec.mergedKwargs(payload.toolArgs, params: spec.params)
        let values = ApprovalEditSpec.initialValues(spec, toolArgs: payload.toolArgs)
        var rows = spec.fields.compactMap { field -> ApprovalInfoRow? in
            let value = (values[field.key] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else { return nil }
            let oneLine = value.split(whereSeparator: \.isNewline).joined(separator: " ")
            return ApprovalInfoRow(label: field.label, value: oneLine)
        }
        rows.append(contentsOf: spec.readOnly(merged))
        var seen = Set<String>()
        return Array(rows.filter { seen.insert($0.label).inserted }.prefix(4))
    }

    /// Approve needs the same required fields as the full card.
    static func isComplete(_ payload: ApprovalRequestPayload) -> Bool {
        guard let spec = ApprovalEditSpec.spec(for: payload.toolName, toolArgs: payload.toolArgs) else {
            return true
        }
        return spec.isComplete(ApprovalEditSpec.initialValues(spec, toolArgs: payload.toolArgs))
    }
}

// MARK: - Bundled card logic (#76, pure)

enum BundleCard {
    static func toolArgs(_ action: ApprovalAction) -> JSONValue {
        .object(["args": .array([]), "kwargs": action.args])
    }

    static func spec(_ action: ApprovalAction) -> ApprovalEditSpec? {
        ApprovalEditSpec.spec(for: action.tool, toolArgs: toolArgs(action))
    }

    static func title(_ action: ApprovalAction) -> String {
        if let label = action.label?.trimmingCharacters(in: .whitespaces), !label.isEmpty {
            return label
        }
        return action.tool
    }

    static func windowHeight(_ actions: [ApprovalAction]) -> CGFloat {
        let fields = actions.reduce(CGFloat(0)) { total, action in
            guard let spec = spec(action) else { return total + 40 }
            return total + 60 + CGFloat(spec.fields.count) * 58 + CGFloat(spec.readOnly([:]).count) * 40
        }
        return min(ApprovalEditSpec.maxWindowHeight, 300 + fields)
    }

    /// At least one action ticked, and every ticked one has its required fields.
    static func isComplete(_ actions: [ApprovalAction], ticks: [Bool], values: [[String: String]]) -> Bool {
        guard ticks.count == actions.count, ticks.contains(true) else { return false }
        for (index, action) in actions.enumerated() where ticks[index] {
            if let spec = spec(action), index < values.count, !spec.isComplete(values[index]) {
                return false
            }
        }
        return true
    }

    /// `{"actions": [{tool, args, enabled, label}, ...]}`: every action, in
    /// order, with the user's tick and edited fields. Always sent, so the
    /// daemon knows which steps were unticked.
    static func editedArgs(
        _ actions: [ApprovalAction], ticks: [Bool], values: [[String: String]]
    ) -> JSONValue {
        let list: [JSONValue] = actions.enumerated().map { index, action in
            let enabled = index < ticks.count ? ticks[index] : (action.enabled ?? true)
            var newArgs: JSONValue? = nil
            if let spec = spec(action), index < values.count,
               case .object(let edited)? = ApprovalEditSpec.editedArgs(
                   spec, toolArgs: toolArgs(action), values: values[index]),
               let kwargs = edited["kwargs"] {
                newArgs = kwargs
            }
            return action.encoded(enabled: enabled, args: newArgs)
        }
        return .object(["actions": .array(list)])
    }
}

// MARK: - Edit spec (pure logic)

/// One editable text field on an approval card. `key` is the tool's real
/// Python parameter name, so edits go back as kwargs the tool accepts.
struct ApprovalEditField: Hashable {
    let key: String
    let label: String
    var placeholder: String = ""
    var multiline: Bool = false
    var required: Bool = false
    var monospaced: Bool = false
}

/// A read-only line on an approval card (e.g. a formatted event time).
struct ApprovalInfoRow: Hashable {
    let label: String
    let value: String
}

/// Describes the editable approval card for one tool, and holds the pure
/// functions that turn the agent's `tool_args` into field values and the
/// user's field values back into an `edited_args` payload.
///
/// `edited_args` is `{"args": [], "kwargs": {...}}`: every positional arg is
/// converted to its keyword (by `params` order), non-editable args keep the
/// agent's value, and editable ones take the user's text. It is nil when no
/// editable field changed, so the daemon runs the agent's original call.
struct ApprovalEditSpec {
    let toolName: String
    /// The tool's parameters in signature order (maps positional args).
    let params: [String]
    let fields: [ApprovalEditField]
    var approveLabel: String = "Approve"
    /// Builds the read-only rows from the merged kwargs.
    var readOnly: ([String: JSONValue]) -> [ApprovalInfoRow] = { _ in [] }

    /// Spec for `toolName`, or nil (read-only JSON view) for other tools and
    /// for calls whose positional args can't be mapped to parameter names.
    static func spec(for toolName: String, toolArgs: JSONValue) -> ApprovalEditSpec? {
        guard let spec = all[toolName] else { return nil }
        if positional(toolArgs).count > spec.params.count { return nil }
        return spec
    }

    var windowHeight: CGFloat {
        let rows = readOnly([:]).count  // count is independent of values
        let fieldHeight = fields.reduce(CGFloat(0)) { $0 + ($1.multiline ? 122 : 58) }
        return min(Self.maxWindowHeight, 350 + fieldHeight + CGFloat(rows) * 46)
    }

    /// Tallest approval card: fits under the menu bar on a 13" display.
    static let maxWindowHeight: CGFloat = 740

    func isComplete(_ values: [String: String]) -> Bool {
        fields.allSatisfy { field in
            !field.required || !(values[field.key] ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    /// Agent args as one kwargs dict: positional args named by `params`,
    /// explicit kwargs win on conflict.
    static func mergedKwargs(_ toolArgs: JSONValue, params: [String]) -> [String: JSONValue] {
        var merged: [String: JSONValue] = [:]
        for (index, value) in positional(toolArgs).enumerated() where index < params.count {
            merged[params[index]] = value
        }
        for (key, value) in keyword(toolArgs) { merged[key] = value }
        return merged
    }

    static func initialValues(_ spec: ApprovalEditSpec, toolArgs: JSONValue) -> [String: String] {
        let merged = mergedKwargs(toolArgs, params: spec.params)
        var values: [String: String] = [:]
        for field in spec.fields { values[field.key] = merged[field.key]?.displayText ?? "" }
        return values
    }

    static func editedArgs(
        _ spec: ApprovalEditSpec, toolArgs: JSONValue, values: [String: String]
    ) -> JSONValue? {
        let original = initialValues(spec, toolArgs: toolArgs)
        let changed = spec.fields.contains { (values[$0.key] ?? "") != (original[$0.key] ?? "") }
        guard changed else { return nil }
        var kwargs = mergedKwargs(toolArgs, params: spec.params)
        for field in spec.fields { kwargs[field.key] = .string(values[field.key] ?? "") }
        return .object(["args": .array([]), "kwargs": .object(kwargs)])
    }

    private static func positional(_ toolArgs: JSONValue) -> [JSONValue] {
        if case .object(let dict) = toolArgs, case .array(let args)? = dict["args"] { return args }
        return []
    }

    private static func keyword(_ toolArgs: JSONValue) -> [String: JSONValue] {
        if case .object(let dict) = toolArgs, case .object(let kw)? = dict["kwargs"] { return kw }
        return [:]
    }

    // MARK: Per-tool specs

    static let all: [String: ApprovalEditSpec] = {
        let specs: [ApprovalEditSpec] = [
            ApprovalEditSpec(
                toolName: "send_imessage",
                params: ["target_number", "message"],
                fields: [
                    .init(key: "target_number", label: "To",
                          placeholder: "+14155551212 or name@icloud.com", monospaced: true),
                    .init(key: "message", label: "Message", multiline: true, required: true),
                ],
                approveLabel: "Approve & Send"
            ),
            ApprovalEditSpec(
                toolName: "open_url",
                params: ["url"],
                fields: [.init(key: "url", label: "URL", placeholder: "https://…",
                               required: true, monospaced: true)],
                approveLabel: "Approve & Open"
            ),
            ApprovalEditSpec(
                toolName: "show_notification",
                params: ["title", "body"],
                fields: [
                    .init(key: "title", label: "Title", required: true),
                    .init(key: "body", label: "Body", multiline: true, required: true),
                ]
            ),
            ApprovalEditSpec(
                toolName: "move_file",
                params: ["src", "dst_dir", "new_name"],
                fields: [
                    .init(key: "src", label: "File", placeholder: "/path/to/file",
                          required: true, monospaced: true),
                    .init(key: "dst_dir", label: "Move to folder", placeholder: "/path/to/folder",
                          required: true, monospaced: true),
                    // #76: optional rename; empty keeps the file's own name.
                    .init(key: "new_name", label: "File name", placeholder: "Keep the current name",
                          monospaced: true),
                ],
                approveLabel: "Approve & Move"
            ),
            ApprovalEditSpec(
                toolName: "create_event",
                params: ["title", "start", "end", "calendar", "location", "notes"],
                fields: [
                    .init(key: "title", label: "Title", required: true),
                    .init(key: "location", label: "Location"),
                    .init(key: "notes", label: "Notes", multiline: true),
                ],
                readOnly: { kw in
                    let (date, time) = EventTimeFormat.dateAndRange(
                        start: kw["start"]?.displayText ?? "", end: kw["end"]?.displayText ?? "")
                    return [
                        .init(label: "Date", value: date),
                        .init(label: "Time", value: time),
                        .init(label: "Calendar",
                              value: nonEmpty(kw["calendar"]?.displayText) ?? "Default calendar"),
                    ]
                }
            ),
            ApprovalEditSpec(
                toolName: "create_reminder",
                params: ["title", "due", "list_name", "notes"],
                fields: [
                    .init(key: "title", label: "Title", required: true),
                    .init(key: "notes", label: "Notes", multiline: true),
                ],
                readOnly: { kw in
                    [
                        .init(label: "Due",
                              value: EventTimeFormat.single(kw["due"]?.displayText ?? "")
                                  ?? "No due date"),
                        .init(label: "List",
                              value: nonEmpty(kw["list_name"]?.displayText) ?? "Default list"),
                    ]
                }
            ),
            ApprovalEditSpec(
                toolName: "create_note",
                params: ["title", "body", "folder"],
                fields: [
                    .init(key: "title", label: "Title", required: true),
                    .init(key: "body", label: "Body", multiline: true),
                    .init(key: "folder", label: "Folder", placeholder: "Default folder"),
                ]
            ),
            ApprovalEditSpec(
                toolName: "append_note",
                params: ["title", "text"],
                fields: [
                    .init(key: "title", label: "Note title (exact)", required: true),
                    .init(key: "text", label: "Text to append", multiline: true, required: true),
                ]
            ),
            // Drafts only (#63): the daemon has no tool that sends mail. The
            // card says so, and the button never says "Send".
            ApprovalEditSpec(
                toolName: "draft_email",
                params: ["to", "subject", "body", "cc", "reply_to_message_id"],
                fields: [
                    .init(key: "to", label: "To", placeholder: "name@example.com, …",
                          required: true, monospaced: true),
                    .init(key: "cc", label: "Cc", placeholder: "name@example.com, …",
                          monospaced: true),
                    .init(key: "subject", label: "Subject"),
                    .init(key: "body", label: "Body", multiline: true, required: true),
                ],
                approveLabel: "Approve & Create Draft",
                readOnly: { kw in
                    let replyId = nonEmpty(kw["reply_to_message_id"]?.displayText)
                    let isReply = replyId != nil && replyId != "0"
                    let kind = isReply
                        ? "Opens a reply draft in Mail, threaded under the original message."
                        : "Opens a new draft in Mail."
                    return [
                        .init(label: "Draft only — nothing is sent",
                              value: "\(kind) You review it and press Send in Mail yourself."),
                    ]
                }
            ),
            ApprovalEditSpec(
                toolName: "run_shortcut",
                params: ["name", "input_text"],
                fields: [.init(key: "input_text", label: "Input text", multiline: true)],
                approveLabel: "Approve & Run",
                readOnly: { kw in
                    [.init(label: "Shortcut", value: kw["name"]?.displayText ?? "")]
                }
            ),
            ApprovalEditSpec(
                toolName: "open_app",
                params: ["name"],
                fields: [],
                approveLabel: "Approve & Open",
                readOnly: { kw in
                    [.init(label: "App", value: kw["name"]?.displayText ?? "")]
                }
            ),
        ]
        return Dictionary(uniqueKeysWithValues: specs.map { ($0.toolName, $0) })
    }()

    private static func nonEmpty(_ text: String?) -> String? {
        guard let text, !text.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return text
    }
}

/// Formats the daemon's ISO 8601 local datetimes for the approval card.
enum EventTimeFormat {
    /// ("Tue, Nov 10, 2026", "12:00 PM – 1:00 PM"). An empty end means one
    /// hour after start, matching `create_event`. Unparseable input is shown raw.
    static func dateAndRange(start: String, end: String) -> (String, String) {
        guard let begin = parse(start) else { return (start.isEmpty ? "—" : start, "—") }
        if isDateOnly(start) { return (dateText(begin), "All day") }
        let finish = parse(end) ?? begin.addingTimeInterval(3600)
        let sameDay = Calendar.current.isDate(begin, inSameDayAs: finish)
        let endText = sameDay ? timeText(finish) : "\(dateText(finish)) \(timeText(finish))"
        return (dateText(begin), "\(timeText(begin)) – \(endText)")
    }

    /// "Tue, Nov 10, 2026 at 12:00 PM", or nil for an empty value.
    static func single(_ value: String) -> String? {
        let text = value.trimmingCharacters(in: .whitespaces)
        if text.isEmpty { return nil }
        guard let date = parse(text) else { return text }
        return isDateOnly(text) ? dateText(date) : "\(dateText(date)) at \(timeText(date))"
    }

    static func parse(_ value: String) -> Date? {
        var text = value.trimmingCharacters(in: .whitespaces)
        if text.isEmpty { return nil }
        if text.hasSuffix("Z") { text = String(text.dropLast()) + "+00:00" }
        let iso = ISO8601DateFormatter()
        for options: ISO8601DateFormatter.Options in [
            [.withInternetDateTime], [.withInternetDateTime, .withFractionalSeconds],
        ] {
            iso.formatOptions = options
            if let date = iso.date(from: text) { return date }
        }
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.timeZone = .current
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm:ss",
                       "yyyy-MM-dd HH:mm", "yyyy-MM-dd"] {
            local.dateFormat = format
            if let date = local.date(from: text) { return date }
        }
        return nil
    }

    private static func isDateOnly(_ value: String) -> Bool {
        let text = value.trimmingCharacters(in: .whitespaces)
        return text.count == 10 && !text.contains("T")
    }

    private static func dateText(_ date: Date) -> String {
        date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().year())
    }

    private static func timeText(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .shortened)
    }
}

// MARK: - JSONValue conveniences

private extension JSONValue {
    /// Text for a field: strings as-is, other scalars printed, null empty.
    var displayText: String {
        switch self {
        case .string(let s): return s
        case .null: return ""
        case .bool(let b): return b ? "true" : "false"
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        case .array, .object: return prettyPrinted()
        }
    }
}
