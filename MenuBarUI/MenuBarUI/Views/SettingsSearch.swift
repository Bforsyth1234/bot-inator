import Foundation

// #135: the Settings categories, the table of sections in them, and the
// search filter. Plain Foundation (no SwiftUI) so the table and the filter
// can be checked outside the app. `SettingsView` draws each section from
// its `SettingsSectionID`; this table decides the order and what a search
// finds. A new Settings section is one case below, one row in `entries`
// and one arm in `SettingsView.section(_:)`.

/// A sidebar entry in Settings, top to bottom.
enum SettingsCategory: String, CaseIterable, Identifiable, Hashable {
    case general, models, features, delivery, permissions, memory, personalModel

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .models: return "Models"
        case .features: return "Features"
        case .delivery: return "Delivery & iPhone"
        case .permissions: return "Permissions & consent"
        case .memory: return "Memory & data"
        case .personalModel: return "Personal model"
        }
    }

    /// SF Symbol for the sidebar row.
    var symbol: String {
        switch self {
        case .general: return "gearshape"
        case .models: return "cpu"
        case .features: return "sparkles"
        case .delivery: return "iphone"
        case .permissions: return "hand.raised"
        case .memory: return "externaldrive"
        case .personalModel: return "person.crop.circle"
        }
    }
}

/// One Settings section. Every case has exactly one row in
/// `SettingsSearch.entries` and one view in `SettingsView`.
enum SettingsSectionID: String, CaseIterable, Hashable {
    // General
    case appearance, app, mainWindow
    // Models
    case provider, presets, vision, webSearch
    case localModels
    // Features
    case dailyBrief, weeklyReport, meetingPrep, meetingNotes, quickAsk, watchMe, watches
    // Delivery & iPhone
    case delivery, remote
    // Permissions & consent
    case permissions, rules, alwaysAllow
    // Memory & data
    case knowledge, activityHistory, actionHistory, privacyLedger
    // Personal model
    case trainingData, personalModel
}

struct SettingsEntry: Identifiable, Hashable {
    let id: SettingsSectionID
    let category: SettingsCategory
    /// The section's name as the user sees it.
    let title: String
    /// Words a user might type for this section (matched with the title).
    let keywords: [String]
}

enum SettingsSearch {
    /// Every Settings section, in display order (categories in
    /// `SettingsCategory` order, sections in the order shown).
    static let entries: [SettingsEntry] = [
        // General
        SettingsEntry(id: .appearance, category: .general, title: "Appearance",
                      keywords: ["text size", "font", "bigger", "smaller", "larger", "zoom",
                                 "readability", "look"]),
        SettingsEntry(id: .app, category: .general, title: "App",
                      keywords: ["login", "start at login", "launch", "startup", "daemon",
                                 "bundled", "update", "new version", "check for updates", "restart"]),
        SettingsEntry(id: .mainWindow, category: .general, title: "Main window",
                      keywords: ["window", "dock", "cmd-tab", "open at launch", "menu bar", "icon"]),

        // Models
        SettingsEntry(id: .provider, category: .models, title: "Inference provider",
                      keywords: ["model", "provider", "engine", "llm", "ai", "local", "mlx", "cloud",
                                 "groq", "openai", "main", "analysis", "drafting", "per engine",
                                 "running now", "restart", "email text", "message text",
                                 "personal text", "privacy"]),
        SettingsEntry(id: .presets, category: .models, title: "Provider settings",
                      keywords: ["groq", "openai", "compatible", "preset", "api key", "key",
                                 "base url", "endpoint", "model", "list models", "test", "cloud"]),
        SettingsEntry(id: .localModels, category: .models, title: "Local models",
                      keywords: ["mlx", "model", "local", "hugging face", "folder", "main", "analysis", "drafting", "memory"]),
        SettingsEntry(id: .vision, category: .models, title: "Vision (look at my screen)",
                      keywords: ["vision", "screen", "screenshot", "look at my screen", "image",
                                 "model", "capture", "window"]),
        SettingsEntry(id: .webSearch, category: .models, title: "Web search",
                      keywords: ["web", "search", "internet", "online", "brave", "api key", "key",
                                 "browse", "pages"]),

        // Features
        SettingsEntry(id: .dailyBrief, category: .features, title: "Morning brief",
                      keywords: ["daily brief", "morning", "summary", "digest", "schedule", "time"]),
        SettingsEntry(id: .weeklyReport, category: .features, title: "Weekly report",
                      keywords: ["weekly", "week", "report", "summary", "run now"]),
        SettingsEntry(id: .meetingPrep, category: .features, title: "Meeting prep",
                      keywords: ["meeting", "calendar", "prep", "agenda", "before a meeting"]),
        SettingsEntry(id: .meetingNotes, category: .features, title: "Meeting notes",
                      keywords: ["meeting", "notes", "transcript", "record", "microphone", "mic",
                                 "dictation", "speech", "audio"]),
        SettingsEntry(id: .quickAsk, category: .features, title: "Quick ask",
                      keywords: ["quick ask", "hotkey", "shortcut", "keyboard", "ask", "screen context",
                                 "accessibility"]),
        SettingsEntry(id: .watchMe, category: .features, title: "Watch me",
                      keywords: ["record", "recording", "shortcut", "hotkey", "demonstrate", "teach",
                                 "show it", "task"]),
        SettingsEntry(id: .watches, category: .features, title: "Watch for…",
                      keywords: ["watch for", "watches", "alert", "notify", "monitor", "trigger",
                                 "look out"]),

        // Delivery & iPhone
        SettingsEntry(id: .delivery, category: .delivery, title: "Delivery",
                      keywords: ["suggestions", "cards", "daily limit", "cap", "ideas", "inbox",
                                 "never watch", "apps", "ignore apps", "quiet", "interruptions"]),
        SettingsEntry(id: .remote, category: .delivery, title: "iPhone remote",
                      keywords: ["iphone", "phone", "imessage", "messages", "remote", "text me",
                                 "away", "mobile"]),

        // Permissions & consent
        SettingsEntry(id: .permissions, category: .permissions, title: "Permissions",
                      keywords: ["macos", "permission", "accessibility", "automation", "full disk access",
                                 "screen recording", "contacts", "grant", "system settings", "privacy"]),
        SettingsEntry(id: .rules, category: .permissions, title: "Standing rules",
                      keywords: ["rule", "always do this", "automatic", "consent", "revoke"]),
        SettingsEntry(id: .alwaysAllow, category: .permissions, title: "Always allow",
                      keywords: ["always allow", "auto approve", "auto-approve", "countdown", "revoke",
                                 "consent", "trust", "approval"]),

        // Memory & data
        SettingsEntry(id: .knowledge, category: .memory, title: "What the assistant knows",
                      keywords: ["knowledge", "index", "search", "mail", "email", "messages", "imessage",
                                 "calendar", "notes", "files", "folders", "documents", "browser",
                                 "browser history", "safari", "chrome", "chatgpt", "claude", "ai chats",
                                 "import", "past chats", "meeting notes", "forget everything",
                                 "personal gpt", "privacy"]),
        SettingsEntry(id: .activityHistory, category: .memory, title: "Activity history",
                      keywords: ["activity", "history", "apps used", "timeline", "clear", "retention"]),
        SettingsEntry(id: .actionHistory, category: .memory, title: "History: what the assistant did",
                      keywords: ["undo", "history", "actions", "approved", "revert", "30 days"]),
        SettingsEntry(id: .privacyLedger, category: .memory, title: "Privacy ledger",
                      keywords: ["privacy", "ledger", "egress", "network", "cloud", "what left this mac",
                                 "sent", "upload", "data"]),

        // Personal model
        SettingsEntry(id: .trainingData, category: .personalModel, title: "Training data",
                      keywords: ["training", "train", "fine-tune", "fine tune", "lora", "corpus",
                                 "dataset", "personal model", "progress"]),
        SettingsEntry(id: .personalModel, category: .personalModel, title: "Personal model",
                      keywords: ["personal model", "adapter", "lora", "your models", "original",
                                 "go back", "model"]),
    ]

    /// Lower-cased, trimmed, inner runs of whitespace collapsed to one space.
    static func normalize(_ text: String) -> String {
        text.lowercased()
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
    }

    /// The entries a query finds, in table order. An empty (or all-space)
    /// query finds every entry. Each word of the query must appear in the
    /// title or a keyword (substring, case ignored). The category name is
    /// not searched, so "iphone" finds iPhone remote, not all of
    /// "Delivery & iPhone".
    static func filter(_ query: String, in entries: [SettingsEntry] = entries) -> [SettingsEntry] {
        let words = normalize(query).split(separator: " ").map(String.init)
        guard !words.isEmpty else { return entries }
        return entries.filter { entry in
            let haystack = ([entry.title] + entry.keywords).map(normalize).joined(separator: "\n")
            return words.allSatisfy { haystack.contains($0) }
        }
    }

    /// Entries of one category, in table order.
    static func entries(in category: SettingsCategory) -> [SettingsEntry] {
        entries.filter { $0.category == category }
    }

    /// Group entries under their categories, in category order; empty
    /// categories are left out.
    static func grouped(_ entries: [SettingsEntry]) -> [(category: SettingsCategory, entries: [SettingsEntry])] {
        SettingsCategory.allCases.compactMap { category in
            let inCategory = entries.filter { $0.category == category }
            return inCategory.isEmpty ? nil : (category, inCategory)
        }
    }
}
