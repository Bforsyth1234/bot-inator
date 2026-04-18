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

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case approved
        case userNote = "user_note"
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
}

// MARK: - Discriminated Union

enum WSMessage: Codable, Hashable {
    case thought(seq: Int, timestamp: Date, payload: ThoughtPayload)
    case approvalRequest(seq: Int, timestamp: Date, payload: ApprovalRequestPayload)
    case approvalResponse(seq: Int, timestamp: Date, payload: ApprovalResponsePayload)
    case status(seq: Int, timestamp: Date, payload: StatusPayload)
    case command(seq: Int, timestamp: Date, payload: CommandPayload)

    var seq: Int {
        switch self {
        case .thought(let seq, _, _), .approvalRequest(let seq, _, _),
             .approvalResponse(let seq, _, _), .status(let seq, _, _),
             .command(let seq, _, _):
            return seq
        }
    }

    var timestamp: Date {
        switch self {
        case .thought(_, let ts, _), .approvalRequest(_, let ts, _),
             .approvalResponse(_, let ts, _), .status(_, let ts, _),
             .command(_, let ts, _):
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
        case status
        case command
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
        case .status:
            self = .status(seq: seq, timestamp: timestamp,
                           payload: try container.decode(StatusPayload.self, forKey: .payload))
        case .command:
            self = .command(seq: seq, timestamp: timestamp,
                            payload: try container.decode(CommandPayload.self, forKey: .payload))
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
        case .status(_, _, let payload):
            try container.encode(MessageType.status, forKey: .type)
            try container.encode(payload, forKey: .payload)
        case .command(_, _, let payload):
            try container.encode(MessageType.command, forKey: .type)
            try container.encode(payload, forKey: .payload)
        }
    }
}
