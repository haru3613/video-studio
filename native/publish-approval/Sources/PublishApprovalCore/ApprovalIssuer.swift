import Darwin
import Foundation
import Security

public final class ApprovalLeafStore {
    public static var productionRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/video-studio/publish-attestations", isDirectory: true)
    }

    private let root: URL

    public init(root: URL = ApprovalLeafStore.productionRoot) {
        self.root = root
    }

    public func write(reference: String, leaf: JSONValue) throws {
        guard let opaqueID = Self.opaqueID(reference) else {
            throw ApprovalError.store("attestation_ref is malformed")
        }
        try ensureRoot()
        let data = try CanonicalJSON.encode(leaf)
        let filename = opaqueID + ".json"
        let directoryFD = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directoryFD >= 0 else {
            throw ApprovalError.store("protected attestation directory is unavailable")
        }
        defer { Darwin.close(directoryFD) }
        let fd = filename.withCString {
            Darwin.openat(directoryFD, $0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        }
        guard fd >= 0 else {
            if errno == EEXIST {
                throw ApprovalError.store("attestation leaf already exists; refusing to overwrite it")
            }
            throw ApprovalError.store("could not create the attestation leaf")
        }
        var keep = false
        defer {
            Darwin.close(fd)
            if !keep {
                filename.withCString { _ = Darwin.unlinkat(directoryFD, $0, 0) }
            }
        }
        guard fchmod(fd, 0o600) == 0 else {
            throw ApprovalError.store("could not restrict attestation leaf permissions")
        }
        try data.withUnsafeBytes { rawBuffer in
            guard let base = rawBuffer.baseAddress else { return }
            var written = 0
            while written < data.count {
                let count = Darwin.write(fd, base.advanced(by: written), data.count - written)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw ApprovalError.store("could not write the attestation leaf")
                }
                written += count
            }
        }
        guard fsync(fd) == 0, fsync(directoryFD) == 0 else {
            throw ApprovalError.store("could not durably store the attestation leaf")
        }
        keep = true
    }

    private func ensureRoot() throws {
        var status = stat()
        if lstat(root.path, &status) != 0 {
            guard errno == ENOENT else {
                throw ApprovalError.store("could not inspect the protected attestation directory")
            }
            do {
                try FileManager.default.createDirectory(
                    at: root,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw ApprovalError.store("could not create the protected attestation directory")
            }
            guard chmod(root.path, 0o700) == 0, lstat(root.path, &status) == 0 else {
                throw ApprovalError.store("could not restrict the protected attestation directory")
            }
        }
        guard status.st_mode & S_IFMT == S_IFDIR,
              status.st_uid == getuid(),
              status.st_mode & 0o777 == 0o700
        else {
            throw ApprovalError.store("protected attestation directory must be current-user owned mode 0700 and not a symlink")
        }
    }

    private static func opaqueID(_ reference: String) -> String? {
        let prefix = "attestation:"
        guard reference.hasPrefix(prefix) else { return nil }
        let value = String(reference.dropFirst(prefix.count))
        guard value.range(
            of: #"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"#,
            options: .regularExpression
        ) != nil else { return nil }
        return value
    }
}

public final class ApprovalIssuer {
    private let key: ApprovalSigningKey
    private let store: ApprovalLeafStore
    private let now: () -> Date

    public init(
        key: ApprovalSigningKey,
        store: ApprovalLeafStore,
        now: @escaping () -> Date = Date.init
    ) {
        self.key = key
        self.store = store
        self.now = now
    }

    public func issue(requestData: Data) throws -> String {
        try issue(verifiedRequest: verify(requestData))
    }

    public func verify(_ requestData: Data) throws -> VerifiedApprovalRequest {
        try ApprovalRequestVerifier(now: now).verify(requestData)
    }

    public func issue(verifiedRequest request: VerifiedApprovalRequest) throws -> String {
        let pin = try key.keyInfo()
        guard pin.algorithm == ApprovalContract.algorithm,
              pin.keyID == request.expectedKeyID,
              pin.publicKeyX963.count == 65,
              pin.publicKeyX963.first == 0x04,
              ApprovalRequestVerifier.sha256Hex(pin.publicKeyX963) == pin.keyID
        else {
            throw ApprovalError.key("the enrolled public key does not match the request pin")
        }

        // The key's sign operation itself triggers the macOS user-presence UI.
        // Check immediately before entering that operation, then again after the
        // user returns so a slow prompt cannot produce an already-expired leaf.
        guard now() < request.expiresAt else {
            throw ApprovalError.invalidRequest("attestation expired before the user-presence prompt")
        }
        let signature = try key.sign(
            request.statement,
            reason: request.authenticationReason,
            expectedKeyID: request.expectedKeyID
        )
        guard now() < request.expiresAt else {
            throw ApprovalError.invalidRequest("attestation expired during the user-presence prompt")
        }

        var leaf = request.immutableAttestation.objectValue!
        leaf["signature_base64"] = .string(signature.base64EncodedString())
        leaf["consumed_at"] = .null
        leaf["consumed_project_id"] = .null
        leaf["consumed_intent_sha256"] = .null
        try store.write(reference: request.attestationReference, leaf: .object(leaf))
        return request.attestationReference
    }

    public func selfTest() throws -> JSONValue {
        let pin = try key.keyInfo()
        var random = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else {
            throw ApprovalError.key("could not create a self-test challenge")
        }
        let challenge = random.map { String(format: "%02x", $0) }.joined()
        let statement = try CanonicalJSON.encode(.object([
            "schema": .string(ApprovalContract.selfTestSchema),
            "challenge": .string(challenge),
        ]))
        let signature = try key.sign(
            statement,
            reason: ApprovalContract.selfTestReason,
            expectedKeyID: pin.keyID
        )
        guard try SignatureVerification.verifyP256(
            message: statement,
            signature: signature,
            publicKeyX963: pin.publicKeyX963
        ) else {
            throw ApprovalError.key("approval key self-test signature did not verify")
        }
        return .object(["ok": .bool(true), "key_id": .string(pin.keyID)])
    }
}
