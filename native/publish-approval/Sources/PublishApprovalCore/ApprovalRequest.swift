import CryptoKit
import Foundation

public enum ApprovalError: Error, Equatable, CustomStringConvertible {
    case invalidRequest(String)
    case unsafeFile(String)
    case key(String)
    case store(String)

    public var description: String {
        switch self {
        case let .invalidRequest(message): "invalid approval request: \(message)"
        case let .unsafeFile(message): "unsafe approval file: \(message)"
        case let .key(message): "approval key error: \(message)"
        case let .store(message): "attestation store error: \(message)"
        }
    }
}

public struct PublicKeyPin: Equatable, Sendable {
    public let algorithm: String
    public let keyID: String
    public let publicKeyX963: Data

    public init(algorithm: String, keyID: String, publicKeyX963: Data) {
        self.algorithm = algorithm
        self.keyID = keyID
        self.publicKeyX963 = publicKeyX963
    }

    public var json: JSONValue {
        .object([
            "algorithm": .string(algorithm),
            "key_id": .string(keyID),
            "public_key_x963_base64": .string(publicKeyX963.base64EncodedString()),
        ])
    }
}

public struct VerifiedApprovalRequest: Equatable, Sendable {
    public let attestationReference: String
    public let immutableAttestation: JSONValue
    public let statement: Data
    public let expiresAt: Date
    public let expectedKeyID: String
    public let requestedChannelID: String
    public let reviewText: String
    public let authenticationReason: String
}

public enum ApprovalContract {
    // These haru.* strings are deployed wire-protocol versions shared with the
    // Python and Rust verifiers. The public helper namespace is separate, but a
    // schema rename requires a coordinated protocol migration.
    public static let requestSchema = "haru.publish_approval_signing_request.v1"
    public static let intentSchema = "haru.publish_approval.v3"
    public static let attestationSchema = "haru.publish_attestation.v2"
    public static let statementSchema = "haru.publish_authorization_statement.v1"
    public static let action = "youtube.upload.unlisted"
    public static let visibility = "unlisted"
    public static let algorithm = "ecdsa-p256-sha256"
    public static let selfTestSchema = "haru.publish_approval_key_self_test.v1"
    public static let selfTestReason = "Verify the Video Studio approval key; this does not approve or upload a video"

    static let requestKeys: Set<String> = ["schema", "project_root", "intent", "intent_sha256", "attestation"]
    static let intentKeys: Set<String> = [
        "schema", "project_id", "final_sha256", "final_bytes", "metadata_sha256",
        "cover_sha256", "channel_id", "visibility", "warnings_acknowledged",
        "override_reason", "runtime_contract", "render_self_eval", "visual_qa_review",
        "generation", "attestation_ref", "nonce",
    ]
    static let attestationKeys: Set<String> = [
        "schema", "attestation_ref", "project_id", "approval_intent_sha256", "nonce",
        "generation", "issued_at", "expires_at", "channel_id", "visibility", "key_id",
        "signature_algorithm", "project_root_sha256",
    ]
    static let runtimeContractKeys: Set<String> = ["schema", "runtime", "evaluator", "artifact"]
    static let evidenceReferenceKeys: Set<String> = ["path", "sha256", "bytes"]
}

public struct ApprovalRequestVerifier {
    private let now: () -> Date

    public init(now: @escaping () -> Date = Date.init) {
        self.now = now
    }

    public func verify(_ data: Data) throws -> VerifiedApprovalRequest {
        var parser = try StrictJSONParser(data: data)
        let requestValue = try parser.parse()
        let request = try object(requestValue, "request")
        try exactKeys(request, ApprovalContract.requestKeys, "request")
        try equalString(request["schema"], ApprovalContract.requestSchema, "request schema")

        let projectRoot = try string(request["project_root"], "project_root")
        let intentValue = try required(request["intent"], "intent")
        let intent = try object(intentValue, "intent")
        let attestationValue = try required(request["attestation"], "attestation")
        let attestation = try object(attestationValue, "attestation")
        try exactKeys(intent, ApprovalContract.intentKeys, "intent")
        try exactKeys(attestation, ApprovalContract.attestationKeys, "attestation")

        try equalString(intent["schema"], ApprovalContract.intentSchema, "intent schema")
        try equalString(attestation["schema"], ApprovalContract.attestationSchema, "attestation schema")
        let requestedChannelID = try youtubeChannelID(
            attestation["channel_id"],
            "attestation channel_id"
        )
        try equalString(attestation["visibility"], ApprovalContract.visibility, "attestation visibility")
        try equalString(attestation["signature_algorithm"], ApprovalContract.algorithm, "signature_algorithm")
        let projectRootDigest = Self.sha256Hex(Data(projectRoot.utf8))
        try equalString(attestation["project_root_sha256"], projectRootDigest, "project_root_sha256")

        let intentDigest = Self.sha256Hex(try CanonicalJSON.encode(intentValue))
        let declaredIntentDigest = try lowercaseHex(request["intent_sha256"], "intent_sha256")
        guard intentDigest == declaredIntentDigest else {
            throw ApprovalError.invalidRequest("intent_sha256 does not match the canonical intent")
        }
        try equalString(attestation["approval_intent_sha256"], declaredIntentDigest, "attestation approval_intent_sha256")

        let projectID = try nonemptyString(intent["project_id"], "intent project_id")
        guard projectID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"#, options: .regularExpression) != nil else {
            throw ApprovalError.invalidRequest("project_id is malformed")
        }
        try equalString(attestation["project_id"], projectID, "attestation project_id")
        try equalString(intent["channel_id"], requestedChannelID, "intent channel_id")
        try equalString(intent["visibility"], ApprovalContract.visibility, "intent visibility")

        let reference = try nonemptyString(attestation["attestation_ref"], "attestation_ref")
        guard reference.range(
            of: #"^attestation:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"#,
            options: .regularExpression
        ) != nil else {
            throw ApprovalError.invalidRequest("attestation_ref must contain a canonical lowercase UUID")
        }
        try equalString(intent["attestation_ref"], reference, "intent attestation_ref")
        let nonce = try lowercaseHex(attestation["nonce"], "nonce")
        try equalString(intent["nonce"], nonce, "intent nonce")
        let generation = try positiveInteger(attestation["generation"], "attestation generation")
        guard try positiveInteger(intent["generation"], "intent generation") == generation else {
            throw ApprovalError.invalidRequest("intent generation does not match attestation generation")
        }

        let keyID = try lowercaseHex(attestation["key_id"], "key_id")
        _ = try lowercaseHex(intent["final_sha256"], "final_sha256")
        _ = try positiveInteger(intent["final_bytes"], "final_bytes")
        _ = try lowercaseHex(intent["metadata_sha256"], "metadata_sha256")
        _ = try lowercaseHex(intent["cover_sha256"], "cover_sha256")
        try validateRuntimeContract(intent["runtime_contract"])
        let warnings = try stringArray(intent["warnings_acknowledged"], "warnings_acknowledged")
        try validateWarnings(warnings, overrideReason: intent["override_reason"])

        let issuedAt = try timestamp(attestation["issued_at"], "issued_at")
        let expiresAt = try timestamp(attestation["expires_at"], "expires_at")
        let lifetime = expiresAt.timeIntervalSince(issuedAt)
        guard lifetime > 0, lifetime <= 300 else {
            throw ApprovalError.invalidRequest("attestation lifetime must be from 1 through 300 seconds")
        }
        let current = now()
        guard issuedAt <= current else {
            throw ApprovalError.invalidRequest("attestation is future-dated")
        }
        guard current < expiresAt else {
            throw ApprovalError.invalidRequest("attestation has expired")
        }

        let reader = try SecureProjectReader(projectRoot: projectRoot, projectID: projectID)
        let final = try reader.hash(relativePath: "output/final.mp4")
        let expectedFinalSHA = try string(intent["final_sha256"], "final_sha256")
        let expectedFinalBytes = try positiveInteger(intent["final_bytes"], "final_bytes")
        guard final.sha256 == expectedFinalSHA,
              final.bytes == expectedFinalBytes
        else {
            throw ApprovalError.invalidRequest("output/final.mp4 does not match the intent")
        }
        let cover = try reader.hash(relativePath: "output/cover.png")
        let expectedCoverSHA = try string(intent["cover_sha256"], "cover_sha256")
        guard cover.sha256 == expectedCoverSHA else {
            throw ApprovalError.invalidRequest("output/cover.png does not match the intent")
        }
        let metadataData = try reader.read(relativePath: "publish-metadata.json", maximumBytes: 1_048_576)
        let expectedMetadataSHA = try string(intent["metadata_sha256"], "metadata_sha256")
        guard Self.sha256Hex(metadataData) == expectedMetadataSHA else {
            throw ApprovalError.invalidRequest("publish-metadata.json does not match the intent")
        }
        let metadata = try validateMetadata(metadataData, projectID: projectID)

        let selfEval = try validateEvidence(intent["render_self_eval"], name: "render_self_eval", reader: reader)
        let visualReview = try validateEvidence(intent["visual_qa_review"], name: "visual_qa_review", reader: reader)

        let statement = JSONValue.object([
            "schema": .string(ApprovalContract.statementSchema),
            "action": .string(ApprovalContract.action),
            "attestation": attestationValue,
        ])
        let reviewText = makeReviewText(
            projectID: projectID,
            title: metadata.title,
            description: metadata.description,
            warnings: warnings,
            finalSHA: final.sha256,
            metadataSHA: expectedMetadataSHA,
            coverSHA: cover.sha256,
            selfEvalSHA: selfEval.sha256,
            visualReviewSHA: visualReview.sha256,
            requestedChannelID: requestedChannelID
        )
        return VerifiedApprovalRequest(
            attestationReference: reference,
            immutableAttestation: attestationValue,
            statement: try CanonicalJSON.encode(statement),
            expiresAt: expiresAt,
            expectedKeyID: keyID,
            requestedChannelID: requestedChannelID,
            reviewText: reviewText,
            authenticationReason: "核准 Video Studio 專案 \(projectID) 上傳到頻道 \(requestedChannelID)，intent \(declaredIntentDigest.prefix(16))。完整內容已顯示於核准視窗。"
        )
    }

    private func validateEvidence(
        _ value: JSONValue?,
        name: String,
        reader: SecureProjectReader
    ) throws -> FileDigest {
        let reference = try object(try required(value, name), name)
        try exactKeys(reference, ApprovalContract.evidenceReferenceKeys, name)
        let path = try nonemptyString(reference["path"], "\(name) path")
        let expectedSHA = try lowercaseHex(reference["sha256"], "\(name) sha256")
        let expectedBytes = try positiveInteger(reference["bytes"], "\(name) bytes")
        let actual = try reader.hash(boundReferencePath: path)
        guard actual.sha256 == expectedSHA, actual.bytes == expectedBytes else {
            throw ApprovalError.invalidRequest("\(name) bytes do not match the intent reference")
        }
        return actual
    }

    private func validateMetadata(_ data: Data, projectID: String) throws -> (title: String, description: String) {
        var parser = try StrictJSONParser(data: data)
        let metadata = try object(parser.parse(), "publish metadata")
        try equalString(metadata["schema"], "haru.publish_metadata.v1", "publish metadata schema")
        try equalString(metadata["project"], projectID, "publish metadata project")
        return (
            try nonemptyString(metadata["title"], "publish metadata title"),
            try nonemptyString(metadata["description"], "publish metadata description")
        )
    }

    private func validateRuntimeContract(_ value: JSONValue?) throws {
        let contract = try object(try required(value, "runtime_contract"), "runtime_contract")
        try exactKeys(contract, ApprovalContract.runtimeContractKeys, "runtime_contract")
        try equalString(contract["schema"], "haru.project_runtime_contract.v1", "runtime contract schema")
        for field in ["runtime", "evaluator", "artifact"] {
            _ = try nonemptyString(contract[field], "runtime_contract \(field)")
        }
    }

    private func validateWarnings(_ warnings: [String], overrideReason: JSONValue?) throws {
        guard warnings.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            throw ApprovalError.invalidRequest("warnings_acknowledged contains an empty warning")
        }
        guard warnings == warnings.sorted(), Set(warnings).count == warnings.count else {
            throw ApprovalError.invalidRequest("warnings_acknowledged must be sorted and unique")
        }
        if warnings.isEmpty {
            guard overrideReason == .null else {
                throw ApprovalError.invalidRequest("override_reason must be null when there are no warnings")
            }
        } else {
            let reason = try nonemptyString(overrideReason, "override_reason")
            guard reason.count >= 12 else {
                throw ApprovalError.invalidRequest("override_reason must contain at least 12 characters")
            }
        }
    }

    private func makeReviewText(
        projectID: String,
        title: String,
        description: String,
        warnings: [String],
        finalSHA: String,
        metadataSHA: String,
        coverSHA: String,
        selfEvalSHA: String,
        visualReviewSHA: String,
        requestedChannelID: String
    ) -> String {
        let warningText = warnings.isEmpty ? "無" : warnings.map { "• \($0)" }.joined(separator: "\n")
        return """
        核准這次 Video Studio YouTube 上傳
        專案：\(projectID)
        標題：\(title)
        說明：\(description)
        請求的頻道：\(requestedChannelID)
        注意：頻道 ID 來自待簽請求；最終服務仍會比對安裝時固定的頻道政策。
        可見度：不公開（\(ApprovalContract.visibility)）
        待確認警告：\n\(warningText)

        稽核資訊
        Final SHA-256: \(finalSHA)
        Metadata SHA-256: \(metadataSHA)
        Cover SHA-256: \(coverSHA)
        Render self-eval SHA-256: \(selfEvalSHA)
        Visual QA review SHA-256: \(visualReviewSHA)
        """
    }

    private func required(_ value: JSONValue?, _ name: String) throws -> JSONValue {
        guard let value else { throw ApprovalError.invalidRequest("\(name) is required") }
        return value
    }

    private func object(_ value: JSONValue, _ name: String) throws -> [String: JSONValue] {
        guard case let .object(object) = value else {
            throw ApprovalError.invalidRequest("\(name) must be an object")
        }
        return object
    }

    private func exactKeys(_ object: [String: JSONValue], _ expected: Set<String>, _ name: String) throws {
        guard Set(object.keys) == expected else {
            throw ApprovalError.invalidRequest("\(name) has missing or unexpected fields")
        }
    }

    private func string(_ value: JSONValue?, _ name: String) throws -> String {
        guard case let .string(string)? = value else {
            throw ApprovalError.invalidRequest("\(name) must be a string")
        }
        return string
    }

    private func nonemptyString(_ value: JSONValue?, _ name: String) throws -> String {
        let value = try string(value, name)
        guard !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ApprovalError.invalidRequest("\(name) must not be empty")
        }
        return value
    }

    private func equalString(_ value: JSONValue?, _ expected: String, _ name: String) throws {
        guard try string(value, name) == expected else {
            throw ApprovalError.invalidRequest("\(name) does not match the required value")
        }
    }

    private func positiveInteger(_ value: JSONValue?, _ name: String) throws -> Int64 {
        guard case let .integer(integer)? = value, integer > 0 else {
            throw ApprovalError.invalidRequest("\(name) must be a positive integer")
        }
        return integer
    }

    private func lowercaseHex(_ value: JSONValue?, _ name: String) throws -> String {
        let value = try string(value, name)
        guard value.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else {
            throw ApprovalError.invalidRequest("\(name) must be 64 lowercase hexadecimal characters")
        }
        return value
    }

    private func youtubeChannelID(_ value: JSONValue?, _ name: String) throws -> String {
        let value = try string(value, name)
        guard value.range(
            of: #"^UC[A-Za-z0-9_-]{22}$"#,
            options: .regularExpression
        ) != nil else {
            throw ApprovalError.invalidRequest(
                "\(name) must be a structurally valid YouTube channel ID"
            )
        }
        return value
    }

    private func stringArray(_ value: JSONValue?, _ name: String) throws -> [String] {
        guard case let .array(array)? = value else {
            throw ApprovalError.invalidRequest("\(name) must be an array")
        }
        return try array.map { try string($0, name) }
    }

    private func timestamp(_ value: JSONValue?, _ name: String) throws -> Date {
        let value = try string(value, name)
        guard value.range(
            of: #"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"#,
            options: .regularExpression
        ) != nil else {
            throw ApprovalError.invalidRequest("\(name) must be second-precision UTC RFC3339 with +00:00")
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withColonSeparatorInTimeZone]
        guard let date = formatter.date(from: value) else {
            throw ApprovalError.invalidRequest("\(name) is not a valid timestamp")
        }
        return date
    }

    public static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
