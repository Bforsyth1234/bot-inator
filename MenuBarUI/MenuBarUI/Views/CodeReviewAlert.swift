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
    /// red X sends a denial (#15). `outsideSandbox` is the off-by-default
    /// "Run outside the sandbox" checkbox (#104).
    let onDecision: (_ approved: Bool, _ editedCode: String?, _ note: String,
                     _ outsideSandbox: Bool) -> Void

    @State private var draft: String = ""
    @State private var userNote: String = ""
    /// #130: the card's chat (replaces the note field when offered).
    @StateObject private var chat = CardChatModel()
    /// #104: off by default; the daemon trusts only this exact source.
    @State private var runOutsideSandbox = false
    /// Repair cards (#90) open on the diff; "Full source" shows the editor.
    @State private var showFullSource = false
    @Environment(\.textSize) private var textSize

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            if !addedNetworkImports.isEmpty { networkWarning }
            if let summary { summarySection(summary) }
            descriptionSection
            if previousCode != nil && !showFullSource {
                diffSection
            } else {
                codeEditorSection
            }
            if chatOn {
                chatSection
            } else {
                noteSection
            }
            // #151: what this tool needs, above the (off by default) checkbox.
            if let needs = payload.needs, !needs.isEmpty {
                ToolNeedsSection(needs: needs, style: .exact)
            }
            trustSection
            Spacer(minLength: 0)
            buttonRow
        }
        .padding(18)
        .frame(width: 680,
               height: min(chatOn ? 900 : 740,
                           (((summary != nil ? 700 : 600) + (chatOn ? 170 : 0)
                             + (payload.needs?.isEmpty == false ? 90 : 0))
                            * textSize.layoutScale).rounded()))
        .onAppear {
            chat.requestId = payload.requestId
            if draft.isEmpty { draft = payload.code }
        }
    }

    /// #130: the daemon offers a chat on this card.
    private var chatOn: Bool { payload.chat == true }

    private var chatSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            if chat.rewriting {
                Label("Rewriting from your chat. The new code goes through the same checks and sandboxed dry run and opens on a new review card; this one can't be approved.",
                      systemImage: "arrow.triangle.2.circlepath")
                    .appFont(.body, weight: .semibold)
                    .foregroundStyle(.orange)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            } else if draft != payload.code {
                Text("The chat sees the code as drafted, not your edits.")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
            }
            CardChatView(model: chat, title: "Ask about this code",
                         placeholder: "What does this line do? Why does it need that app?")
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "wand.and.stars")
                .foregroundStyle(.purple)
                .appFont(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(previousCode != nil ? "Repair Review"
                     : payload.replacesRequestId != nil ? "Code Review (rewritten)" : "Code Review")
                    .appFont(.headline)
                Text(payload.toolName)
                    .appFont(.label, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if previousCode != nil {
                Picker("", selection: $showFullSource) {
                    Text("Changes").tag(false)
                    Text("Full source").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 190)
            }
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

    // MARK: Repair diff (#90)

    /// The installed source a repair replaces; nil for a new tool.
    private var previousCode: String? {
        guard let text = payload.previousCode, !text.isEmpty else { return nil }
        return text
    }

    /// Network imports in the draft whose module the installed code did
    /// not import: a repair may not quietly widen what the tool touches.
    private var addedNetworkImports: [String] {
        guard let previousCode else { return [] }
        return CodeReviewDiff.addedNetworkImports(old: previousCode, new: draft)
    }

    private var networkWarning: some View {
        Label("This repair adds network access: \(addedNetworkImports.joined(separator: ", "))",
              systemImage: "exclamationmark.triangle.fill")
            .appFont(.body, weight: .semibold)
            .foregroundStyle(.orange)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
    }

    private var diffSection: some View {
        let rows = CodeReviewDiff.rows(old: previousCode ?? "", new: draft)
        return VStack(alignment: .leading, spacing: 4) {
            Text("Changes against the installed version")
                .appFont(.secondary, weight: .semibold)
                .foregroundStyle(.secondary)
            ScrollView([.vertical, .horizontal]) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { row in
                        Text(row.display)
                            .appFont(.secondary, design: .monospaced)
                            .foregroundStyle(row.kind == .same || row.kind == .skipped
                                             ? Color.secondary : Color.primary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(row.kind == .added ? Color.green.opacity(0.18)
                                        : row.kind == .removed ? Color.red.opacity(0.18)
                                        : Color.clear)
                    }
                }
                .textSelection(.enabled)
                .padding(6)
            }
            .frame(minHeight: summary != nil ? 200 : 260)
            .background(Color(nsColor: .textBackgroundColor),
                        in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25)))
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

    /// #104: let this reviewed tool run outside the sandbox. Every call still
    /// asks; any later change to the file (edit, repair) turns this off.
    private var trustSection: some View {
        VStack(alignment: .leading, spacing: 2) {
            Toggle(isOn: $runOutsideSandbox) {
                Text("Run outside the sandbox")
                    .appFont(.body)
            }
            .toggleStyle(.checkbox)
            Text(runOutsideSandbox
                 ? "This code will run with your full user access: no sandbox limits on files, apps, programs or network, and its network use is not in the privacy ledger. Every call still asks. Any change to the code turns this off."
                 : "Leave off unless the sandbox blocks something this tool needs (quitting apps, reading Mail). You can change it later in Tool Manager.")
                .appFont(.fine)
                .foregroundStyle(runOutsideSandbox ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var buttonRow: some View {
        HStack {
            Spacer()
            Button(role: .destructive) {
                onDecision(false, nil, chatOn ? "" : userNote, false)
            } label: {
                Label("Deny", systemImage: "xmark.circle.fill")
            }
            .keyboardShortcut(.cancelAction)

            Button {
                let edited: String? =
                    (draft != payload.code) ? draft : nil
                onDecision(true, edited, chatOn ? "" : userNote, runOutsideSandbox)
            } label: {
                Label(previousCode != nil ? "Approve Repair" : "Approve & Install",
                      systemImage: "checkmark.circle.fill")
            }
            // #130: Return in the chat field must never approve the card.
            .keyboardShortcut(chatOn ? nil : KeyboardShortcut.defaultAction)
            .buttonStyle(.borderedProminent)
            // #130: no Approve while a reply or a rewrite could change the code.
            .disabled(draft.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty || chat.blocksApproval)
        }
    }
}

/// Line diff and network-import check for repair cards (#90).
enum CodeReviewDiff {
    enum Kind { case same, added, removed, skipped }

    struct Row: Identifiable {
        let id: Int
        let kind: Kind
        let text: String

        var display: String {
            switch kind {
            case .added: return "+ " + text
            case .removed: return "- " + text
            case .same: return "  " + text
            case .skipped: return "  ⋯ " + text
            }
        }
    }

    /// Mirrors `tools.tool_sandbox.NETWORK_MODULES`.
    static let networkModules: Set<String> = [
        "urllib", "http", "socket", "ssl", "requests", "httpx", "aiohttp",
        "urllib3", "websocket", "websockets", "ftplib", "smtplib", "imaplib",
        "poplib", "xmlrpc",
    ]

    /// Unchanged lines kept around each change; longer runs collapse.
    static let context = 3

    static func rows(old: String, new: String) -> [Row] {
        let a = old.components(separatedBy: "\n")
        let b = new.components(separatedBy: "\n")
        var removed = Set<Int>(), inserted = Set<Int>()
        for change in b.difference(from: a) {
            switch change {
            case .remove(let offset, _, _): removed.insert(offset)
            case .insert(let offset, _, _): inserted.insert(offset)
            }
        }
        var raw: [(Kind, String)] = []
        var i = 0, j = 0
        while i < a.count || j < b.count {
            if i < a.count, removed.contains(i) {
                raw.append((.removed, a[i])); i += 1
            } else if j < b.count, inserted.contains(j) {
                raw.append((.added, b[j])); j += 1
            } else if j < b.count {
                raw.append((.same, b[j])); i += 1; j += 1
            } else {
                i += 1
            }
        }
        let changed = raw.indices.filter { raw[$0].0 != .same }
        var rows: [Row] = []
        var skipped = 0
        func flushSkipped() {
            if skipped > 0 {
                rows.append(Row(id: rows.count, kind: .skipped, text: "\(skipped) unchanged lines"))
                skipped = 0
            }
        }
        for (idx, item) in raw.enumerated() {
            let near = changed.contains { abs($0 - idx) <= context }
            if item.0 == .same && !near {
                skipped += 1
                continue
            }
            flushSkipped()
            rows.append(Row(id: rows.count, kind: item.0, text: item.1))
        }
        flushSkipped()
        return rows
    }

    /// Top-level module names of the network imports in `source`.
    static func networkImports(_ source: String) -> [String: String] {
        var found: [String: String] = [:]
        for line in source.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let words = trimmed.split(whereSeparator: { $0 == " " || $0 == "," })
            guard let head = words.first, head == "import" || head == "from",
                  words.count >= 2 else { continue }
            let names = head == "from" ? [words[1]] : Array(words.dropFirst())
            for name in names where name != "as" {
                let root = String(name.split(separator: ".").first ?? "")
                if networkModules.contains(root), found[root] == nil {
                    found[root] = trimmed
                }
            }
        }
        return found
    }

    /// Import lines in `new` that bring in a network module `old` did not.
    static func addedNetworkImports(old: String, new: String) -> [String] {
        let before = networkImports(old)
        return networkImports(new)
            .filter { before[$0.key] == nil }
            .map(\.value)
            .sorted()
    }
}
