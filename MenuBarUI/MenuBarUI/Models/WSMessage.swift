import Foundation

// MARK: - Payloads

struct ThoughtPayload: Codable, Hashable {
    let eventId: String
    let stage: ThoughtStage
    let content: String

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case stage
        case content
    }
}

enum ThoughtStage: String, Codable, Hashable {
    case eventReceived = "event_received"
    case analysis
    case memory
    case reasoning
    case plan
    case toolResult = "tool_result"
    case complete
}

struct ApprovalRequestPayload: Codable, Hashable {
    let requestId: String
    let eventId: String
    let toolName: String
    let toolArgs: JSONValue
    let reasoning: String
    let timeoutSeconds: Int?
    /// Plain-language summary of a suggested tool (#64). Only set on
    /// `generate_custom_tool` idea cards; nil from older daemons.
    let summary: String?
    /// A prepared offer's actions (#76): one approval runs every ticked one.
    /// nil (older daemons, ordinary cards) keeps the one-call card.
    let actions: [ApprovalAction]?
    /// #83: the daemon's action journal can undo this tool's result. The
    /// presenter shows such a card as a small panel that does not take focus
    /// (still a card: Approve / Deny / Edit). nil (older daemons) = false.
    var reversible: Bool? = nil
    /// #81: the card was shaped by outside text this turn read ("Read this
    /// turn: email from ...", "to: from the email, not from you"). nil on
    /// unmarked turns and from older daemons.
    var originNotes: [String]? = nil

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case eventId = "event_id"
        case toolName = "tool_name"
        case toolArgs = "tool_args"
        case reasoning
        case timeoutSeconds = "timeout_seconds"
        case summary
        case actions
        case reversible
        case originNotes = "origin_notes"
    }
}

/// One action of a bundled approval card (#76). `args` are the tool's
/// kwargs. The card sends the list back in `edited_args["actions"]` with
/// `enabled` as the user ticked it; the daemon runs only ticked actions.
struct ApprovalAction: Codable, Hashable {
    let tool: String
    let args: JSONValue
    let enabled: Bool?
    let label: String?

    /// `{"tool", "args", "enabled", "label"}` for `edited_args["actions"]`.
    func encoded(enabled: Bool, args newArgs: JSONValue? = nil) -> JSONValue {
        .object([
            "tool": .string(tool),
            "args": newArgs ?? args,
            "enabled": .bool(enabled),
            "label": .string(label ?? ""),
        ])
    }
}

struct ApprovalResponsePayload: Codable, Hashable {
    let requestId: String
    let approved: Bool
    let userNote: String?
    /// Args edited by the user in the approval UI. Mirrors the shape of
    /// `ApprovalRequestPayload.toolArgs` — typically
    /// `{"args": [...], "kwargs": {...}}`. `nil` means "use the values the
    /// agent originally proposed".
    let editedArgs: JSONValue?

    init(requestId: String, approved: Bool, userNote: String?,
         editedArgs: JSONValue? = nil) {
        self.requestId = requestId
        self.approved = approved
        self.userNote = userNote
        self.editedArgs = editedArgs
    }

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case approved
        case userNote = "user_note"
        case editedArgs = "edited_args"
    }
}

struct StatusPayload: Codable, Hashable {
    let state: DaemonState
    let modelLoaded: String?
    let listenersActive: [String]?
    let version: String?
    /// Liveness per supervised listener (e.g. `nsworkspace`), #19.
    let listenerHealth: [String: ListenerHealth]?
    /// Provider per engine role (`main`, `analysis`, `drafting`), #31.
    let engineProviders: [String: String]?
    /// True when personal text may reach cloud engines
    /// (`AGENT_CLOUD_PERSONAL_TEXT=1`), #31.
    let cloudPersonalText: Bool?
    /// #87 delivery: Pause, offers held for a better moment, and the Inbox.
    /// `pausedUntil` is ISO 8601 or "indefinite". nil from older daemons.
    let paused: Bool?
    let pausedUntil: String?
    let heldCount: Int?
    let inboxCount: Int?

    init(state: DaemonState, modelLoaded: String? = nil,
         listenersActive: [String]? = nil, version: String? = nil,
         listenerHealth: [String: ListenerHealth]? = nil,
         engineProviders: [String: String]? = nil,
         cloudPersonalText: Bool? = nil,
         paused: Bool? = nil, pausedUntil: String? = nil,
         heldCount: Int? = nil, inboxCount: Int? = nil) {
        self.state = state
        self.modelLoaded = modelLoaded
        self.listenersActive = listenersActive
        self.version = version
        self.listenerHealth = listenerHealth
        self.engineProviders = engineProviders
        self.cloudPersonalText = cloudPersonalText
        self.paused = paused
        self.pausedUntil = pausedUntil
        self.heldCount = heldCount
        self.inboxCount = inboxCount
    }

    enum CodingKeys: String, CodingKey {
        case state
        case modelLoaded = "model_loaded"
        case listenersActive = "listeners_active"
        case version
        case listenerHealth = "listener_health"
        case engineProviders = "engine_providers"
        case cloudPersonalText = "cloud_personal_text"
        case paused
        case pausedUntil = "paused_until"
        case heldCount = "held_count"
        case inboxCount = "inbox_count"
    }
}

struct ListenerHealth: Codable, Hashable {
    let alive: Bool?
    let pid: Int?
    let restartCount: Int?
    let lastError: String?
    let lastExitCode: Int?
    let uptimeS: Double?

    enum CodingKeys: String, CodingKey {
        case alive, pid
        case restartCount = "restart_count"
        case lastError = "last_error"
        case lastExitCode = "last_exit_code"
        case uptimeS = "uptime_s"
    }
}

/// Server→client banner (#19). The daemon publishes each `id` once per run
/// and replays active notices on connect; a newer notice with the same id
/// replaces the old one.
struct NoticePayload: Codable, Hashable, Identifiable {
    let id: String
    let level: NoticeLevel?
    let title: String
    let body: String
    let action: NoticeAction?
}

enum NoticeLevel: String, Codable, Hashable {
    case info, warning, error
}

struct NoticeAction: Codable, Hashable {
    /// Only `open_settings` today: opens `url` (a System Settings pane).
    let kind: String
    let url: String
}

/// Server→client hint that `GET /api/tools` changed (#29).
struct ToolsChangedPayload: Codable, Hashable {
    let reason: String
    let toolNames: [String]?

    enum CodingKeys: String, CodingKey {
        case reason
        case toolNames = "tool_names"
    }
}

enum DaemonState: String, Codable, Hashable {
    case starting
    case loadingModel = "loading_model"
    case ready
    case processing
    case error
}

struct CommandPayload: Codable, Hashable {
    let action: CommandAction
    /// Only for `pause_listeners` (#87); nil means "until I resume".
    /// `PauseDuration` lives in `OfferInboxView.swift`.
    let duration: PauseDuration?

    init(action: CommandAction, duration: PauseDuration? = nil) {
        self.action = action
        self.duration = duration
    }
}

enum CommandAction: String, Codable, Hashable {
    case pauseListeners = "pause_listeners"
    case resumeListeners = "resume_listeners"
    case reloadModel = "reload_model"
    case clearMemory = "clear_memory"
    case reloadDynamicTools = "reload_dynamic_tools"
}

/// Server→client request to review AI-generated Python tool source.
/// Sent by the meta-tool generator after it has drafted a module and
/// passed its static safety checks.
struct CodeApprovalRequestPayload: Codable, Hashable {
    let requestId: String
    let eventId: String
    let toolName: String
    let description: String
    let code: String
    let timeoutSeconds: Int?
    /// Plain-language summary shown above the code (#64); nil from older
    /// daemons.
    let summary: String?
    /// #90: the installed source a repair replaces; the review card shows a
    /// diff against it. nil for a new tool and from older daemons.
    var previousCode: String? = nil

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case eventId = "event_id"
        case toolName = "tool_name"
        case description
        case code
        case timeoutSeconds = "timeout_seconds"
        case summary
        case previousCode = "previous_code"
    }
}

struct CodeApprovalResponsePayload: Codable, Hashable {
    let requestId: String
    let approved: Bool
    /// Full source text if the user hand-edited the draft; `nil` means the
    /// server should persist the originally-proposed code.
    let editedCode: String?
    let userNote: String?

    init(requestId: String, approved: Bool,
         editedCode: String? = nil, userNote: String? = nil) {
        self.requestId = requestId
        self.approved = approved
        self.editedCode = editedCode
        self.userNote = userNote
    }

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case approved
        case editedCode = "edited_code"
        case userNote = "user_note"
    }
}

/// Client→server chat turn sent by the user from the chat window.
/// `messageId` is generated client-side and reused as the `event_id`
/// on every incoming `thought` frame emitted while the daemon handles
/// the turn, so the UI can group thoughts under the right user bubble.
struct UserMessagePayload: Codable, Hashable {
    let messageId: String
    let text: String
    /// What the user was looking at (#74 quick-ask). Nil from the Chat
    /// window; omitted from the JSON when nil, so older daemons are fine.
    let context: ChatContextPayload?

    init(messageId: String, text: String, context: ChatContextPayload? = nil) {
        self.messageId = messageId
        self.text = text
        self.context = context
    }

    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case text
        case context
    }
}

/// Quick-ask context (#74): front app, window title, and the selected text
/// or clipboard only when the user chose to include it. The daemon puts it
/// in that one turn's prompt; it is not stored.
struct ChatContextPayload: Codable, Hashable {
    var frontApp: String? = nil
    var windowTitle: String? = nil
    var selection: String? = nil
    var selectionSource: SelectionSource? = nil

    enum SelectionSource: String, Codable, Hashable {
        case selection
        case clipboard
        /// #89: "include this window's text". Sent with no `selection`;
        /// the daemon reads the window itself. Older daemons reject the
        /// value, so send it only to a daemon that has `read_front_window`.
        case window
    }

    var isEmpty: Bool {
        frontApp == nil && windowTitle == nil && selection == nil
    }

    enum CodingKeys: String, CodingKey {
        case frontApp = "front_app"
        case windowTitle = "window_title"
        case selection
        case selectionSource = "selection_source"
    }
}

// MARK: - Calendar through the app (#60)

/// EKAuthorizationStatus names, matching the daemon's `tools.calendar_access`.
enum CalendarAuthorization: String, Codable, Hashable {
    case notDetermined = "not_determined"
    case restricted
    case denied
    case fullAccess = "full_access"
    case writeOnly = "write_only"
}

enum CalendarOp: String, Codable, Hashable {
    case list
    case create
    case requestAccess = "request_access"
    /// #83 (undo only): `{event_id}`. `get` answers `events` = [] or one
    /// event; `delete` removes that one event. Only the daemon's action
    /// journal sends them.
    case get
    case delete
}

/// Server→client: run one EventKit operation for the daemon. `args` is
/// `{start, end, calendars?}` for `list`, `{title, start, end, calendar?,
/// location?, notes?}` for `create` (ISO 8601 with offset).
struct CalendarRequestPayload: Codable, Hashable {
    let requestId: String
    let op: CalendarOp
    let args: JSONValue?
    let timeoutSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case op, args
        case timeoutSeconds = "timeout_seconds"
    }
}

/// One event in the #42 shape the daemon's calendar tools return.
struct CalendarEventDTO: Codable, Hashable {
    let id: String
    let title: String
    let start: String
    let end: String
    let calendar: String
    let location: String
    let notes: String
    let url: String
    let allDay: Bool
    /// #80: people on the event (rooms and resources left out); `[]` for a
    /// solo event. Optional so the frame stays the same shape for daemons
    /// older than #80, which ignore it.
    var attendees: [CalendarAttendeeDTO]? = nil

    enum CodingKeys: String, CodingKey {
        case id, title, start, end, calendar, location, notes, url, attendees
        case allDay = "all_day"
    }
}

/// One `EKParticipant` (#80). `email` is the address from its `mailto:`
/// URL ("" when it has none).
struct CalendarAttendeeDTO: Codable, Hashable {
    let name: String
    let email: String
    let isSelf: Bool

    enum CodingKeys: String, CodingKey {
        case name, email
        case isSelf = "is_self"
    }
}

/// Client→server answer to a `calendar_request`.
struct CalendarResponsePayload: Codable, Hashable {
    let requestId: String
    let ok: Bool
    var events: [CalendarEventDTO]? = nil
    var eventId: String? = nil
    var calendar: String? = nil
    var error: String? = nil
    var authorization: CalendarAuthorization? = nil

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case ok, events
        case eventId = "event_id"
        case calendar, error, authorization
    }
}

/// Client→server: this app serves Calendar, with its current EventKit
/// authorization (read without prompting). Sent on connect and on change.
struct CalendarStatusPayload: Codable, Hashable {
    let authorization: CalendarAuthorization
}

// MARK: - Front-window text through the app (#89)

/// Why a `screen_text_request` read nothing (matches the daemon's
/// `ScreenTextErrorCode`).
enum ScreenTextErrorCode: String, Codable, Hashable {
    case accessibilityOff = "accessibility_off"
    case noWindow = "no_window"
    case neverWatch = "never_watch"
    case secureField = "secure_field"
    case browser
    case ownApp = "own_app"
    case nothingReadable = "nothing_readable"
    case failed
}

/// Server→client: read the focused window's Accessibility text once.
/// `neverWatch` is the daemon's #87 list (bundle ids with a dot, app names
/// without): those apps are refused before anything is read.
struct ScreenTextRequestPayload: Codable, Hashable {
    let requestId: String
    var maxChars: Int? = nil
    var neverWatch: [String]? = nil
    var timeoutSeconds: Int? = nil

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case maxChars = "max_chars"
        case neverWatch = "never_watch"
        case timeoutSeconds = "timeout_seconds"
    }
}

/// Client→server answer to a `screen_text_request`. `text` never holds a
/// secure field's value; on `ok == false` it is nil and `errorCode` says why.
struct ScreenTextResponsePayload: Codable, Hashable {
    let requestId: String
    let ok: Bool
    var app: String? = nil
    var bundleId: String? = nil
    var title: String? = nil
    var text: String? = nil
    var truncated: Bool = false
    var error: String? = nil
    var errorCode: ScreenTextErrorCode? = nil
    var accessibilityTrusted: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case ok, app
        case bundleId = "bundle_id"
        case title, text, truncated, error
        case errorCode = "error_code"
        case accessibilityTrusted = "accessibility_trusted"
    }
}

/// Client→server: this app reads front windows for the daemon, and whether
/// it has Accessibility (`AXIsProcessTrusted`, never prompts).
struct ScreenTextStatusPayload: Codable, Hashable {
    let accessibilityTrusted: Bool

    enum CodingKeys: String, CodingKey {
        case accessibilityTrusted = "accessibility_trusted"
    }
}

// MARK: - Discriminated Union

enum WSMessage: Codable, Hashable {
    case thought(seq: Int, timestamp: Date, payload: ThoughtPayload)
    case approvalRequest(seq: Int, timestamp: Date, payload: ApprovalRequestPayload)
    case approvalResponse(seq: Int, timestamp: Date, payload: ApprovalResponsePayload)
    case codeApprovalRequest(seq: Int, timestamp: Date, payload: CodeApprovalRequestPayload)
    case codeApprovalResponse(seq: Int, timestamp: Date, payload: CodeApprovalResponsePayload)
    case status(seq: Int, timestamp: Date, payload: StatusPayload)
    case notice(seq: Int, timestamp: Date, payload: NoticePayload)
    case toolsChanged(seq: Int, timestamp: Date, payload: ToolsChangedPayload)
    case command(seq: Int, timestamp: Date, payload: CommandPayload)
    case userMessage(seq: Int, timestamp: Date, payload: UserMessagePayload)
    case calendarRequest(seq: Int, timestamp: Date, payload: CalendarRequestPayload)
    case calendarResponse(seq: Int, timestamp: Date, payload: CalendarResponsePayload)
    case calendarStatus(seq: Int, timestamp: Date, payload: CalendarStatusPayload)
    case screenTextRequest(seq: Int, timestamp: Date, payload: ScreenTextRequestPayload)
    case screenTextResponse(seq: Int, timestamp: Date, payload: ScreenTextResponsePayload)
    case screenTextStatus(seq: Int, timestamp: Date, payload: ScreenTextStatusPayload)

    var seq: Int {
        switch self {
        case .thought(let seq, _, _), .approvalRequest(let seq, _, _),
             .approvalResponse(let seq, _, _),
             .codeApprovalRequest(let seq, _, _),
             .codeApprovalResponse(let seq, _, _),
             .status(let seq, _, _), .notice(let seq, _, _),
             .toolsChanged(let seq, _, _), .command(let seq, _, _),
             .userMessage(let seq, _, _),
             .calendarRequest(let seq, _, _), .calendarResponse(let seq, _, _),
             .calendarStatus(let seq, _, _),
             .screenTextRequest(let seq, _, _), .screenTextResponse(let seq, _, _),
             .screenTextStatus(let seq, _, _):
            return seq
        }
    }

    var timestamp: Date {
        switch self {
        case .thought(_, let ts, _), .approvalRequest(_, let ts, _),
             .approvalResponse(_, let ts, _),
             .codeApprovalRequest(_, let ts, _),
             .codeApprovalResponse(_, let ts, _),
             .status(_, let ts, _), .notice(_, let ts, _),
             .toolsChanged(_, let ts, _), .command(_, let ts, _),
             .userMessage(_, let ts, _),
             .calendarRequest(_, let ts, _), .calendarResponse(_, let ts, _),
             .calendarStatus(_, let ts, _),
             .screenTextRequest(_, let ts, _), .screenTextResponse(_, let ts, _),
             .screenTextStatus(_, let ts, _):
            return ts
        }
    }

    private enum CodingKeys: String, CodingKey {
        case type, seq, timestamp, payload
    }

    private enum MessageType: String, Codable {
        case thought
        case approvalRequest = "approval_request"
        case approvalResponse = "approval_response"
        case codeApprovalRequest = "code_approval_request"
        case codeApprovalResponse = "code_approval_response"
        case status
        case notice
        case toolsChanged = "tools_changed"
        case command
        case userMessage = "user_message"
        case calendarRequest = "calendar_request"
        case calendarResponse = "calendar_response"
        case calendarStatus = "calendar_status"
        case screenTextRequest = "screen_text_request"
        case screenTextResponse = "screen_text_response"
        case screenTextStatus = "screen_text_status"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decode(MessageType.self, forKey: .type)
        let seq = try container.decode(Int.self, forKey: .seq)
        let timestamp = try container.decode(Date.self, forKey: .timestamp)

        switch type {
        case .thought:
            self = .thought(seq: seq, timestamp: timestamp,
                            payload: try container.decode(ThoughtPayload.self, forKey: .payload))
        case .approvalRequest:
            self = .approvalRequest(seq: seq, timestamp: timestamp,
                                    payload: try container.decode(ApprovalRequestPayload.self, forKey: .payload))
        case .approvalResponse:
            self = .approvalResponse(seq: seq, timestamp: timestamp,
                                     payload: try container.decode(ApprovalResponsePayload.self, forKey: .payload))
        case .codeApprovalRequest:
            self = .codeApprovalRequest(seq: seq, timestamp: timestamp,
                                        payload: try container.decode(CodeApprovalRequestPayload.self, forKey: .payload))
        case .codeApprovalResponse:
            self = .codeApprovalResponse(seq: seq, timestamp: timestamp,
                                         payload: try container.decode(CodeApprovalResponsePayload.self, forKey: .payload))
        case .status:
            self = .status(seq: seq, timestamp: timestamp,
                           payload: try container.decode(StatusPayload.self, forKey: .payload))
        case .notice:
            self = .notice(seq: seq, timestamp: timestamp,
                           payload: try container.decode(NoticePayload.self, forKey: .payload))
        case .toolsChanged:
            self = .toolsChanged(seq: seq, timestamp: timestamp,
                                 payload: try container.decode(ToolsChangedPayload.self, forKey: .payload))
        case .command:
            self = .command(seq: seq, timestamp: timestamp,
                            payload: try container.decode(CommandPayload.self, forKey: .payload))
        case .userMessage:
            self = .userMessage(seq: seq, timestamp: timestamp,
                                payload: try container.decode(UserMessagePayload.self, forKey: .payload))
        case .calendarRequest:
            self = .calendarRequest(seq: seq, timestamp: timestamp,
                                    payload: try container.decode(CalendarRequestPayload.self, forKey: .payload))
        case .calendarResponse:
            self = .calendarResponse(seq: seq, timestamp: timestamp,
                                     payload: try container.decode(CalendarResponsePayload.self, forKey: .payload))
        case .calendarStatus:
            self = .calendarStatus(seq: seq, timestamp: timestamp,
                                   payload: try container.decode(CalendarStatusPayload.self, forKey: .payload))
        case .screenTextRequest:
            self = .screenTextRequest(seq: seq, timestamp: timestamp,
                                      payload: try container.decode(ScreenTextRequestPayload.self, forKey: .payload))
        case .screenTextResponse:
            self = .screenTextResponse(seq: seq, timestamp: timestamp,
                                       payload: try container.decode(ScreenTextResponsePayload.self, forKey: .payload))
        case .screenTextStatus:
            self = .screenTextStatus(seq: seq, timestamp: timestamp,
                                     payload: try container.decode(ScreenTextStatusPayload.self, forKey: .payload))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(seq, forKey: .seq)
        try container.encode(timestamp, forKey: .timestamp)
        switch self {
        case .thought(_, _, let payload):
            try container.encode(MessageType.thought, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .approvalRequest(_, _, let payload):
            try container.encode(MessageType.approvalRequest, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .approvalResponse(_, _, let payload):
            try container.encode(MessageType.approvalResponse, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .codeApprovalRequest(_, _, let payload):
            try container.encode(MessageType.codeApprovalRequest, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .codeApprovalResponse(_, _, let payload):
            try container.encode(MessageType.codeApprovalResponse, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .status(_, _, let payload):
            try container.encode(MessageType.status, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .notice(_, _, let payload):
            try container.encode(MessageType.notice, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .toolsChanged(_, _, let payload):
            try container.encode(MessageType.toolsChanged, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .command(_, _, let payload):
            try container.encode(MessageType.command, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .userMessage(_, _, let payload):
            try container.encode(MessageType.userMessage, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .calendarRequest(_, _, let payload):
            try container.encode(MessageType.calendarRequest, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .calendarResponse(_, _, let payload):
            try container.encode(MessageType.calendarResponse, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .calendarStatus(_, _, let payload):
            try container.encode(MessageType.calendarStatus, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .screenTextRequest(_, _, let payload):
            try container.encode(MessageType.screenTextRequest, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .screenTextResponse(_, _, let payload):
            try container.encode(MessageType.screenTextResponse, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .screenTextStatus(_, _, let payload):
            try container.encode(MessageType.screenTextStatus, forKey: .type)
            try container.encode(payload, forKey: .payload)
        }
    }
}
