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

    /// The editable card for this tool, or nil for the read-only JSON view.
    private var spec: ApprovalEditSpec? {
        ApprovalEditSpec.spec(for: payload.toolName, toolArgs: payload.toolArgs)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            reasoningSection
            if let spec {
                card(spec)
            } else {
                argsSection
            }
            noteSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 480, height: spec?.windowHeight ?? 460)
        .onAppear(perform: populate)
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
            caption("Reasoning")
            Text(payload.reasoning)
                .font(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var argsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            caption("Arguments")
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
                        .font(.body)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(spec.fields, id: \.key) { field in
                VStack(alignment: .leading, spacing: 4) {
                    caption(field.required ? field.label : "\(field.label) (optional)")
                    if field.multiline {
                        TextEditor(text: binding(field.key))
                            .font(.body)
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
                            .font(field.monospaced
                                  ? .system(.body, design: .monospaced) : .body)
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
            .font(.caption.weight(.semibold))
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
                let edited = spec.flatMap {
                    ApprovalEditSpec.editedArgs($0, toolArgs: payload.toolArgs, values: values)
                }
                onDecision(true, userNote, edited)
            } label: {
                Label(spec?.approveLabel ?? "Approve", systemImage: "checkmark.circle.fill")
            }
            .keyboardShortcut(.defaultAction)
            .buttonStyle(.borderedProminent)
            .disabled(!(spec?.isComplete(values) ?? true))
        }
    }

    private func populate() {
        guard !populated, let spec else { return }
        populated = true
        values = ApprovalEditSpec.initialValues(spec, toolArgs: payload.toolArgs)
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
        let fieldHeight = fields.reduce(CGFloat(0)) { $0 + ($1.multiline ? 116 : 52) }
        return min(720, 330 + fieldHeight + CGFloat(rows) * 40)
    }

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
                params: ["src", "dst_dir"],
                fields: [
                    .init(key: "src", label: "File", placeholder: "/path/to/file",
                          required: true, monospaced: true),
                    .init(key: "dst_dir", label: "Move to folder", placeholder: "/path/to/folder",
                          required: true, monospaced: true),
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
