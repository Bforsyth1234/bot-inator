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

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case eventId = "event_id"
        case toolName = "tool_name"
        case toolArgs = "tool_args"
        case reasoning
        case timeoutSeconds = "timeout_seconds"
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

    enum CodingKeys: String, CodingKey {
        case state
        case modelLoaded = "model_loaded"
        case listenersActive = "listeners_active"
        case version
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

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case eventId = "event_id"
        case toolName = "tool_name"
        case description
        case code
        case timeoutSeconds = "timeout_seconds"
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

    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case text
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
    case command(seq: Int, timestamp: Date, payload: CommandPayload)
    case userMessage(seq: Int, timestamp: Date, payload: UserMessagePayload)

    var seq: Int {
        switch self {
        case .thought(let seq, _, _), .approvalRequest(let seq, _, _),
             .approvalResponse(let seq, _, _),
             .codeApprovalRequest(let seq, _, _),
             .codeApprovalResponse(let seq, _, _),
             .status(let seq, _, _), .command(let seq, _, _),
             .userMessage(let seq, _, _):
            return seq
        }
    }

    var timestamp: Date {
        switch self {
        case .thought(_, let ts, _), .approvalRequest(_, let ts, _),
             .approvalResponse(_, let ts, _),
             .codeApprovalRequest(_, let ts, _),
             .codeApprovalResponse(_, let ts, _),
             .status(_, let ts, _), .command(_, let ts, _),
             .userMessage(_, let ts, _):
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
        case command
        case userMessage = "user_message"
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
        case .command:
            self = .command(seq: seq, timestamp: timestamp,
                            payload: try container.decode(CommandPayload.self, forKey: .payload))
        case .userMessage:
            self = .userMessage(seq: seq, timestamp: timestamp,
                                payload: try container.decode(UserMessagePayload.self, forKey: .payload))
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
        case .command(_, _, let payload):
            try container.encode(MessageType.command, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .userMessage(_, _, let payload):
            try container.encode(MessageType.userMessage, forKey: .type)
            try container.encode(payload, forKey: .payload)
        }
    }
}
