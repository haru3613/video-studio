import CryptoKit
import Darwin
import Foundation

public enum SelfEvalApprovalContract {
    public static let requestSchema = "haru.self_eval_operator_signing_request.v1"
    public static let visionIntentSchema = "haru.self_eval_vision_review_intent.v1"
    public static let humanIntentSchema = "haru.self_eval_human_review_intent.v1"
    public static let visionAttestationSchema = "haru.self_eval_vision_attestation.v2"
    public static let humanAttestationSchema = "haru.self_eval_human_attestation.v2"
    public static let statementSchema = "haru.self_eval_authorization_statement.v1"
    public static let unavailableAction = "declare_vision_unavailable"
    public static let humanReviewAction = "record_human_fallback_review"
    public static let algorithm = "ecdsa-p256-sha256"
    public static let resultPath = "quality-review/render-self-eval/render-self-eval.json"
    public static let humanCapability = "human_structural_attestation.v1"
    public static let configurationProvider = "operator-declared"
    public static let configurationModel = "not-configured"
    public static let configurationCapability = "vision_provider_configuration.v1"

    static let requestKeys: Set<String> = [
        "schema", "project_root", "action", "review_input", "review_intent",
        "review_intent_sha256", "attestation",
    ]
    static let attestationKeys: Set<String> = [
        "schema", "attestation_ref", "action", "project_id", "project_root_sha256",
        "review_intent_sha256", "self_eval_result", "nonce", "generation", "issued_at",
        "expires_at", "key_id", "signature_algorithm",
    ]
    static let reviewInputKeys: Set<String> = [
        "reviewer_kind", "verdict", "reviewed_by", "provider", "model", "capability",
        "notes", "findings",
    ]
    static let referenceKeys: Set<String> = ["path", "sha256", "bytes"]
    static let resultKeys: Set<String> = [
        "schema", "project", "status", "attempt", "max_attempts", "attempt_identity",
        "inputs", "boundary_policy", "boundary_plan", "evaluation", "evidence_index",
        "review", "outcome", "tool", "findings", "verdict", "remediation", "next_action",
        "updated_at",
    ]
    static let findingKeys: Set<String> = [
        "timestamp_seconds", "boundary_id", "category", "severity", "message",
    ]
    static let unavailableKeys: Set<String> = [
        "schema", "project", "attempt", "attempt_identity", "evaluation", "evidence_index",
        "reviewed_by", "provider", "model", "capability", "result", "notes", "recorded_at",
        "authority",
    ]
}

public struct VerifiedSelfEvalApprovalRequest: Equatable, Sendable {
    public let attestationReference: String
    public let immutableAttestation: JSONValue
    public let statement: Data
    public let expiresAt: Date
    public let expectedKeyID: String
    public let reviewText: String
    public let authenticationReason: String
}

public struct SelfEvalApprovalRequestVerifier {
    private let now: () -> Date

    public init(now: @escaping () -> Date = Date.init) {
        self.now = now
    }

    public func verify(_ data: Data) throws -> VerifiedSelfEvalApprovalRequest {
        var parser = try StrictJSONParser(data: data)
        let request = try object(parser.parse(), "request")
        try exactKeys(request, SelfEvalApprovalContract.requestKeys, "request")
        try equalString(request["schema"], SelfEvalApprovalContract.requestSchema, "request schema")

        let projectRoot = try string(request["project_root"], "project_root")
        let action = try string(request["action"], "action")
        let reviewInput = try object(required(request["review_input"], "review_input"), "review_input")
        let intentValue = try required(request["review_intent"], "review_intent")
        let intent = try object(intentValue, "review_intent")
        let attestationValue = try required(request["attestation"], "attestation")
        let attestation = try object(attestationValue, "attestation")
        try exactKeys(reviewInput, SelfEvalApprovalContract.reviewInputKeys, "review_input")
        try exactKeys(attestation, SelfEvalApprovalContract.attestationKeys, "attestation")

        let projectID = try nonemptyString(attestation["project_id"], "attestation project_id")
        guard projectID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"#, options: .regularExpression) != nil else {
            throw ApprovalError.invalidRequest("project_id is malformed")
        }
        let reader = try SecureProjectReader(projectRoot: projectRoot, projectID: projectID)
        try equalString(
            attestation["project_root_sha256"],
            Self.sha256Hex(Data(projectRoot.utf8)),
            "project_root_sha256"
        )
        try equalString(attestation["action"], action, "attestation action")
        try equalString(attestation["signature_algorithm"], SelfEvalApprovalContract.algorithm, "signature_algorithm")

        let reference = try nonemptyString(attestation["attestation_ref"], "attestation_ref")
        guard reference.range(
            of: #"^self-eval-attestation:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"#,
            options: .regularExpression
        ) != nil else {
            throw ApprovalError.invalidRequest("attestation_ref must contain a canonical lowercase UUID")
        }
        _ = try lowercaseHex(attestation["nonce"], "nonce")
        let keyID = try lowercaseHex(attestation["key_id"], "key_id")
        _ = try positiveInteger(attestation["generation"], "generation")

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

        let declaredIntentDigest = try lowercaseHex(request["review_intent_sha256"], "review_intent_sha256")
        let intentDigest = Self.sha256Hex(try CanonicalJSON.encode(intentValue))
        guard declaredIntentDigest == intentDigest else {
            throw ApprovalError.invalidRequest("review_intent_sha256 does not match the canonical intent")
        }
        try equalString(attestation["review_intent_sha256"], intentDigest, "attestation review_intent_sha256")

        let currentResultRef = try validateReference(
            attestation["self_eval_result"],
            name: "self_eval_result",
            expectedPath: SelfEvalApprovalContract.resultPath,
            reader: reader
        )
        let resultData = try reader.read(relativePath: SelfEvalApprovalContract.resultPath, maximumBytes: 1_048_576)
        var resultParser = try StrictJSONParser(data: resultData)
        let result = try object(resultParser.parse(), "current self-eval result")
        try exactKeys(result, SelfEvalApprovalContract.resultKeys, "current self-eval result")
        try equalString(result["schema"], "haru.render_self_eval.v1", "current self-eval schema")
        try equalString(result["project"], projectID, "current self-eval project")
        try equalString(result["status"], "needs_human", "current self-eval status")
        try equalString(result["verdict"], "needs_human", "current self-eval verdict")
        guard result["max_attempts"] == .integer(3) else {
            throw ApprovalError.invalidRequest("current self-eval max_attempts is invalid")
        }
        guard result["review"] == .null, result["outcome"] == .null else {
            throw ApprovalError.invalidRequest("current self-eval result is already sealed")
        }
        let attempt = try positiveInteger(result["attempt"], "current self-eval attempt")
        let attemptIdentity = try lowercaseHex(result["attempt_identity"], "current attempt_identity")
        let attemptDirectory = "quality-review/render-self-eval/attempts/attempt-\(String(format: "%02d", attempt))"
        let evaluation = try validateReference(
            result["evaluation"],
            name: "evaluation",
            expectedPath: "\(attemptDirectory)/evaluation.json",
            reader: reader
        )
        let evidenceIndex = try validateReference(
            result["evidence_index"],
            name: "evidence_index",
            expectedPath: "\(attemptDirectory)/evidence-index.json",
            reader: reader
        )

        let expectedIntent = try makeExpectedIntent(
            action: action,
            projectID: projectID,
            attempt: attempt,
            attemptIdentity: attemptIdentity,
            evaluation: evaluation.value,
            evidenceIndex: evidenceIndex.value,
            reviewInput: reviewInput,
            declaredIntent: intent,
            reader: reader
        )
        guard intentValue == expectedIntent else {
            throw ApprovalError.invalidRequest("review_intent does not bind the current self-eval evidence")
        }

        let expectedSchema: String
        if action == SelfEvalApprovalContract.unavailableAction {
            expectedSchema = SelfEvalApprovalContract.visionAttestationSchema
        } else if action == SelfEvalApprovalContract.humanReviewAction {
            expectedSchema = SelfEvalApprovalContract.humanAttestationSchema
        } else {
            throw ApprovalError.invalidRequest("operator self-eval action is not allowed")
        }
        try equalString(attestation["schema"], expectedSchema, "attestation schema")

        let statement = JSONValue.object([
            "schema": .string(SelfEvalApprovalContract.statementSchema),
            "action": .string(action),
            "attestation": attestationValue,
        ])
        let reviewer = try nonemptyString(reviewInput["reviewed_by"], "reviewed_by")
        let verdict = try nonemptyString(reviewInput["verdict"], "verdict")
        let notes = try string(reviewInput["notes"], "notes")
        let findings = try array(reviewInput["findings"], "findings")
        let findingsJSON = try String(
            decoding: CanonicalJSON.encode(.array(findings)),
            as: UTF8.self
        )
        let reviewText = """
        Video Studio render self-evaluation operator action
        Action: \(action)
        Project: \(projectID)
        Attempt: \(attempt)
        Reviewer: \(reviewer)
        Verdict: \(verdict)
        Notes / availability reason: \(notes)
        Findings (\(findings.count)): \(findingsJSON)

        Current self-eval SHA-256: \(currentResultRef.digest.sha256)
        Evaluation SHA-256: \(evaluation.digest.sha256)
        Evidence index SHA-256: \(evidenceIndex.digest.sha256)
        Review intent SHA-256: \(intentDigest)
        """
        let reason = action == SelfEvalApprovalContract.unavailableAction
            ? "確認 Video Studio 專案 \(projectID) 未設定 vision provider；這不是模型審查結果，且不會發布影片。"
            : "確認 Video Studio 專案 \(projectID) 的人工 fallback \(verdict)；這不會發布影片。"
        return VerifiedSelfEvalApprovalRequest(
            attestationReference: reference,
            immutableAttestation: attestationValue,
            statement: try CanonicalJSON.encode(statement),
            expiresAt: expiresAt,
            expectedKeyID: keyID,
            reviewText: reviewText,
            authenticationReason: reason
        )
    }

    private func makeExpectedIntent(
        action: String,
        projectID: String,
        attempt: Int64,
        attemptIdentity: String,
        evaluation: JSONValue,
        evidenceIndex: JSONValue,
        reviewInput: [String: JSONValue],
        declaredIntent: [String: JSONValue],
        reader: SecureProjectReader
    ) throws -> JSONValue {
        let kind = try string(reviewInput["reviewer_kind"], "reviewer_kind")
        let verdict = try string(reviewInput["verdict"], "verdict")
        let reviewedBy = try nonemptyString(reviewInput["reviewed_by"], "reviewed_by")
        let provider = try string(reviewInput["provider"], "provider")
        let model = try string(reviewInput["model"], "model")
        let capability = try string(reviewInput["capability"], "capability")
        let notes = try string(reviewInput["notes"], "notes")
        let findings = try validateFindings(reviewInput["findings"], verdict: verdict)

        var expected: [String: JSONValue] = [
            "project_id": .string(projectID),
            "attempt": .integer(attempt),
            "attempt_identity": .string(attemptIdentity),
            "evaluation": evaluation,
            "evidence_index": evidenceIndex,
            "reviewer_kind": .string(kind),
            "verdict": .string(verdict),
            "reviewed_by": .string(reviewedBy),
            "provider": .string(provider),
            "model": .string(model),
            "capability": .string(capability),
            "notes": .string(notes),
            "findings": .array(findings),
        ]
        if action == SelfEvalApprovalContract.unavailableAction {
            guard kind == "vision", verdict == "unavailable",
                  provider == SelfEvalApprovalContract.configurationProvider,
                  model == SelfEvalApprovalContract.configurationModel,
                  capability == SelfEvalApprovalContract.configurationCapability,
                  notes.trimmingCharacters(in: .whitespacesAndNewlines).count >= 12,
                  findings.isEmpty
            else {
                throw ApprovalError.invalidRequest("operator declaration must describe missing vision provider configuration")
            }
            expected["schema"] = .string(SelfEvalApprovalContract.visionIntentSchema)
        } else if action == SelfEvalApprovalContract.humanReviewAction {
            guard kind == "human_fallback", verdict == "pass" || verdict == "fail",
                  capability == SelfEvalApprovalContract.humanCapability
            else {
                throw ApprovalError.invalidRequest("operator human fallback review is malformed")
            }
            let unavailable = try validateReference(
                declaredIntent["vision_unavailable"],
                name: "vision_unavailable",
                expectedPath: "quality-review/render-self-eval/attempts/attempt-\(String(format: "%02d", attempt))/vision-unavailable.json",
                reader: reader
            )
            let unavailableData = try reader.read(
                relativePath: unavailable.path,
                maximumBytes: 1_048_576
            )
            var unavailableParser = try StrictJSONParser(data: unavailableData)
            let unavailableValue = try object(unavailableParser.parse(), "vision-unavailable receipt")
            try exactKeys(
                unavailableValue,
                SelfEvalApprovalContract.unavailableKeys,
                "vision-unavailable receipt"
            )
            try equalString(
                unavailableValue["schema"],
                "haru.render_self_eval_vision_unavailable.v1",
                "vision-unavailable receipt schema"
            )
            try equalString(unavailableValue["project"], projectID, "vision-unavailable project")
            guard unavailableValue["attempt"] == .integer(attempt),
                  unavailableValue["attempt_identity"] == .string(attemptIdentity),
                  unavailableValue["evaluation"] == evaluation,
                  unavailableValue["evidence_index"] == evidenceIndex
            else {
                throw ApprovalError.invalidRequest("vision-unavailable receipt does not bind the current attempt")
            }
            try equalString(unavailableValue["result"], "unavailable", "vision-unavailable result")
            let unavailableAuthority = try object(
                required(unavailableValue["authority"], "vision-unavailable authority"),
                "vision-unavailable authority"
            )
            let unavailableAuthoritySchema = try string(
                unavailableAuthority["schema"],
                "vision-unavailable authority schema"
            )
            guard unavailableAuthoritySchema == SelfEvalApprovalContract.visionAttestationSchema
                || unavailableAuthoritySchema == "haru.self_eval_vision_attestation.v1"
            else {
                throw ApprovalError.invalidRequest("vision-unavailable authority schema is unsupported")
            }
            expected["schema"] = .string(SelfEvalApprovalContract.humanIntentSchema)
            expected["vision_unavailable"] = unavailable.value
        } else {
            throw ApprovalError.invalidRequest("operator self-eval action is not allowed")
        }
        return .object(expected)
    }

    private func validateFindings(_ value: JSONValue?, verdict: String) throws -> [JSONValue] {
        let findings = try array(value, "findings")
        var previous: (timestamp: Double, strings: [String])?
        for findingValue in findings {
            let finding = try object(findingValue, "finding")
            try exactKeys(finding, SelfEvalApprovalContract.findingKeys, "finding")
            guard let timestamp = finding["timestamp_seconds"]?.numberValue,
                  timestamp.isFinite, timestamp >= 0
            else {
                throw ApprovalError.invalidRequest("finding timestamp_seconds must be a non-negative finite number")
            }
            let strings = try ["boundary_id", "category", "severity", "message"].map {
                try nonemptyString(finding[$0], "finding \($0)")
            }
            if let previous,
               timestamp < previous.timestamp
                   || (timestamp == previous.timestamp
                       && strings.lexicographicallyPrecedes(previous.strings)) {
                throw ApprovalError.invalidRequest("findings must use canonical sorted order")
            }
            previous = (timestamp, strings)
        }
        guard (verdict == "pass" && findings.isEmpty)
            || (verdict == "fail" && !findings.isEmpty)
            || (verdict == "unavailable" && findings.isEmpty)
        else {
            throw ApprovalError.invalidRequest("findings do not match the verdict")
        }
        return findings
    }

    private struct ValidatedReference {
        let value: JSONValue
        let path: String
        let digest: FileDigest
    }

    private func validateReference(
        _ value: JSONValue?,
        name: String,
        expectedPath: String? = nil,
        reader: SecureProjectReader
    ) throws -> ValidatedReference {
        let value = try required(value, name)
        let reference = try object(value, name)
        try exactKeys(reference, SelfEvalApprovalContract.referenceKeys, name)
        let path = try nonemptyString(reference["path"], "\(name) path")
        if let expectedPath, path != expectedPath {
            throw ApprovalError.invalidRequest("\(name) path is not current")
        }
        let sha = try lowercaseHex(reference["sha256"], "\(name) sha256")
        let bytes = try positiveInteger(reference["bytes"], "\(name) bytes")
        let actual = try reader.hash(boundReferencePath: path)
        guard actual.sha256 == sha, actual.bytes == bytes else {
            throw ApprovalError.invalidRequest("\(name) bytes do not match the reference")
        }
        return ValidatedReference(value: value, path: path, digest: actual)
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

    private func array(_ value: JSONValue?, _ name: String) throws -> [JSONValue] {
        guard case let .array(array)? = value else {
            throw ApprovalError.invalidRequest("\(name) must be an array")
        }
        return array
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

    private func lowercaseHex(_ value: JSONValue?, _ name: String) throws -> String {
        let value = try string(value, name)
        guard value.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else {
            throw ApprovalError.invalidRequest("\(name) must be 64 lowercase hexadecimal characters")
        }
        return value
    }

    private func positiveInteger(_ value: JSONValue?, _ name: String) throws -> Int64 {
        guard case let .integer(integer)? = value, integer > 0 else {
            throw ApprovalError.invalidRequest("\(name) must be a positive integer")
        }
        return integer
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

public final class SelfEvalLeafStore {
    public static var productionRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/video-studio/self-eval-attestations", isDirectory: true)
    }

    private let root: URL

    public init(root: URL = SelfEvalLeafStore.productionRoot) {
        self.root = root
    }

    public func write(reference: String, leaf: JSONValue) throws {
        let prefix = "self-eval-attestation:"
        guard reference.hasPrefix(prefix) else {
            throw ApprovalError.store("self-eval attestation_ref is malformed")
        }
        let opaqueID = String(reference.dropFirst(prefix.count))
        guard opaqueID.range(
            of: #"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"#,
            options: .regularExpression
        ) != nil else {
            throw ApprovalError.store("self-eval attestation_ref is malformed")
        }
        try ensureRoot()
        let data = try CanonicalJSON.encode(leaf)
        let directoryFD = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directoryFD >= 0 else {
            throw ApprovalError.store("protected self-eval attestation directory is unavailable")
        }
        defer { Darwin.close(directoryFD) }
        let filename = opaqueID + ".json"
        let fd = filename.withCString {
            Darwin.openat(directoryFD, $0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        }
        guard fd >= 0 else {
            if errno == EEXIST {
                throw ApprovalError.store("self-eval attestation leaf already exists; refusing to overwrite it")
            }
            throw ApprovalError.store("could not create the self-eval attestation leaf")
        }
        var keep = false
        defer {
            Darwin.close(fd)
            if !keep { filename.withCString { _ = Darwin.unlinkat(directoryFD, $0, 0) } }
        }
        guard fchmod(fd, 0o600) == 0 else {
            throw ApprovalError.store("could not restrict self-eval attestation permissions")
        }
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var written = 0
            while written < data.count {
                let count = Darwin.write(fd, base.advanced(by: written), data.count - written)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw ApprovalError.store("could not write the self-eval attestation leaf")
                }
                written += count
            }
        }
        guard fsync(fd) == 0, fsync(directoryFD) == 0 else {
            throw ApprovalError.store("could not durably store the self-eval attestation leaf")
        }
        keep = true
    }

    private func ensureRoot() throws {
        var status = stat()
        if lstat(root.path, &status) != 0 {
            guard errno == ENOENT else {
                throw ApprovalError.store("could not inspect the protected self-eval attestation directory")
            }
            try FileManager.default.createDirectory(
                at: root,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            guard chmod(root.path, 0o700) == 0, lstat(root.path, &status) == 0 else {
                throw ApprovalError.store("could not restrict the protected self-eval attestation directory")
            }
        }
        guard status.st_mode & S_IFMT == S_IFDIR,
              status.st_uid == getuid(),
              status.st_mode & 0o777 == 0o700
        else {
            throw ApprovalError.store("protected self-eval attestation directory must be current-user owned mode 0700 and not a symlink")
        }
    }
}

public final class SelfEvalApprovalIssuer {
    private let key: ApprovalSigningKey
    private let store: SelfEvalLeafStore
    private let now: () -> Date

    public init(
        key: ApprovalSigningKey,
        store: SelfEvalLeafStore,
        now: @escaping () -> Date = Date.init
    ) {
        self.key = key
        self.store = store
        self.now = now
    }

    public func verify(_ requestData: Data) throws -> VerifiedSelfEvalApprovalRequest {
        try SelfEvalApprovalRequestVerifier(now: now).verify(requestData)
    }

    public func issue(verifiedRequest request: VerifiedSelfEvalApprovalRequest) throws -> String {
        let pin = try key.keyInfo()
        guard pin.algorithm == SelfEvalApprovalContract.algorithm,
              pin.keyID == request.expectedKeyID,
              pin.publicKeyX963.count == 65,
              pin.publicKeyX963.first == 0x04,
              SelfEvalApprovalRequestVerifier.sha256Hex(pin.publicKeyX963) == pin.keyID
        else {
            throw ApprovalError.key("the enrolled public key does not match the self-eval request pin")
        }
        guard now() < request.expiresAt else {
            throw ApprovalError.invalidRequest("self-eval attestation expired before the user-presence prompt")
        }
        let signature = try key.sign(
            request.statement,
            reason: request.authenticationReason,
            expectedKeyID: request.expectedKeyID
        )
        guard now() < request.expiresAt else {
            throw ApprovalError.invalidRequest("self-eval attestation expired during the user-presence prompt")
        }
        var leaf = request.immutableAttestation.objectValue!
        leaf["signature_base64"] = .string(signature.base64EncodedString())
        leaf["consumed_at"] = .null
        leaf["consumed_project_id"] = .null
        leaf["consumed_intent_sha256"] = .null
        try store.write(reference: request.attestationReference, leaf: .object(leaf))
        return request.attestationReference
    }
}
