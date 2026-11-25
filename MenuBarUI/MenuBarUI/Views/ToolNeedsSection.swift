import SwiftUI

/// #151: what a generated tool needs to work, said on its cards up front.
///
/// Shared by the code review card (`CodeReviewAlert`, `.exact`: the reasons
/// read from the code) and the idea card (`ApprovalAlert` for
/// `generate_custom_tool`, `.likely`: one "will likely need…" line guessed
/// from the idea's words).
///
/// It only EXPLAINS (invariant 7b, #104): it never touches the "Run outside
/// the sandbox" checkbox, which stays off by default. A Grant button per
/// macOS permission not granted yet runs the Permissions checklist's own
/// action (#59, `PermissionsViewModel.request(permissionId:)`) or opens the
/// System Settings pane; it never approves, denies or closes the card.
struct ToolNeedsSection: View {
    enum Style { case exact, likely }

    let needs: [ToolNeed]
    let style: Style

    @StateObject private var permissions = PermissionsViewModel()

    private var outside: [ToolNeed] { needs.filter(\.isOutsideSandbox) }
    private var permissionNeeds: [ToolNeed] {
        var seen = Set<String>()
        return needs.filter { !$0.isOutsideSandbox && seen.insert($0.kind).inserted }
    }

    var body: some View {
        if needs.isEmpty {
            EmptyView()
        } else {
            switch style {
            case .exact: exactBody
            case .likely: likelyBody
            }
        }
    }

    // MARK: Code card

    private var exactBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !outside.isEmpty {
                Label("This tool needs to run outside the sandbox to work",
                      systemImage: "lock.open.fill")
                    .appFont(.body, weight: .semibold)
                    .foregroundStyle(.orange)
                ForEach(Array(outside.enumerated()), id: \.offset) { _, need in
                    Text("• " + Self.sentence(need.reason))
                        .appFont(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Approve still installs it, sandboxed, with the box below off; those calls will fail until you turn it on, here or later in Tool Manager. Only turn it on if you trust this code.")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(permissionNeeds, id: \.kind) { need in
                permissionRow(need, lead: outside.isEmpty ? "It needs" : "It also needs")
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
    }

    // MARK: Idea card

    private var likelyBody: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(likelyLine, systemImage: "info.circle")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(permissionNeeds.filter { !isGranted($0) }, id: \.kind) { need in
                HStack(spacing: 6) {
                    Text("\(Self.permissionName(need.kind)): \(statusText(need))")
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                    grantButton(need)
                }
            }
        }
    }

    private var likelyLine: String {
        var parts: [String] = []
        if !outside.isEmpty {
            parts.append("to run outside the sandbox ("
                         + outside.map(\.reason).joined(separator: "; ") + ")")
        }
        parts += permissionNeeds.map { Self.permissionName($0.kind) + " access" }
        return "This will likely need " + parts.joined(separator: ", and ") + "."
    }

    // MARK: Permission rows

    private func permissionRow(_ need: ToolNeed, lead: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("\(lead) \(Self.permissionName(need.kind)) access (\(statusText(need))): \(need.reason).")
                .appFont(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            if !isGranted(need) { grantButton(need) }
        }
    }

    @ViewBuilder
    private func grantButton(_ need: ToolNeed) -> some View {
        if let id = need.permissionId, permissions.pending == id {
            ProgressView().controlSize(.small)
        } else {
            // A plain button: never the card's default action, never a decision.
            Button("Grant") { grant(need) }
                .controlSize(.small)
                .disabled(permissions.pending != nil)
                .help("Opens the macOS permission for this. The card stays open.")
        }
    }

    private func grant(_ need: ToolNeed) {
        if let id = need.permissionId {
            Task { await permissions.request(permissionId: id) }
        } else if let url = PermissionsViewModel.settingsURL(forNeedKind: need.kind) {
            NSWorkspace.shared.open(url)
        }
    }

    /// The Permissions row's status once a Grant click refreshed it, else
    /// what the daemon said when it sent the card.
    private func liveStatus(_ need: ToolNeed) -> String? {
        guard let id = need.permissionId else { return nil }
        return permissions.rows.first(where: { $0.id == id })?.status
    }

    private func isGranted(_ need: ToolNeed) -> Bool {
        if let status = liveStatus(need) { return status == "granted" }
        return need.granted == true
    }

    private func statusText(_ need: ToolNeed) -> String {
        if let status = liveStatus(need) {
            switch status {
            case "granted": return "granted"
            case "denied": return "turned off"
            case "not_asked": return "not granted yet"
            default: return "can't tell yet"
            }
        }
        switch need.granted {
        case .some(true): return "granted"
        case .some(false): return "not granted yet"
        case .none: return "can't tell yet"
        }
    }

    // MARK: Words

    static func permissionName(_ kind: String) -> String {
        let automation = "automation:"
        if kind.hasPrefix(automation) {
            return "Automation → " + kind.dropFirst(automation.count)
        }
        switch kind {
        case "contacts": return "Contacts"
        case "calendars": return "Calendars"
        case "reminders": return "Reminders"
        case "full_disk_access": return "Full Disk Access"
        default: return kind
        }
    }

    /// "it quits apps, …" → "It quits apps, …."
    static func sentence(_ reason: String) -> String {
        guard let first = reason.first else { return reason }
        let text = first.uppercased() + reason.dropFirst()
        return text.hasSuffix(".") ? text : text + "."
    }
}
