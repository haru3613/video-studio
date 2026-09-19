import CryptoKit
import Darwin
import Foundation

public struct FileDigest: Equatable, Sendable {
    public let sha256: String
    public let bytes: Int64
}

/// Reads project files through no-follow directory descriptors, closing the
/// gap between a lexical path check and the bytes actually hashed.
public final class SecureProjectReader {
    private let rootFD: Int32
    private let projectID: String

    public init(projectRoot: String, projectID: String) throws {
        guard projectRoot.hasPrefix("/") else {
            throw ApprovalError.unsafeFile("project_root must be absolute")
        }
        let rootComponents = projectRoot.split(separator: "/", omittingEmptySubsequences: false)
        guard rootComponents.first == "",
              rootComponents.dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }),
              "/" + rootComponents.dropFirst().joined(separator: "/") == projectRoot
        else {
            throw ApprovalError.unsafeFile("project_root must be normalized")
        }
        guard URL(fileURLWithPath: projectRoot).lastPathComponent == projectID else {
            throw ApprovalError.unsafeFile("project_root must end in project_id")
        }
        rootFD = try Self.openAbsoluteDirectory(projectRoot)
        self.projectID = projectID
    }

    deinit {
        Darwin.close(rootFD)
    }

    public func hash(relativePath: String) throws -> FileDigest {
        let fd = try openFile(relativePath: relativePath)
        defer { Darwin.close(fd) }
        var hasher = SHA256()
        var total: Int64 = 0
        var buffer = [UInt8](repeating: 0, count: 1_048_576)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count == 0 { break }
            if count < 0 {
                if errno == EINTR { continue }
                throw ApprovalError.unsafeFile("could not read \(relativePath)")
            }
            total += Int64(count)
            hasher.update(data: Data(buffer[0 ..< count]))
        }
        let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        return FileDigest(sha256: digest, bytes: total)
    }

    public func read(relativePath: String, maximumBytes: Int) throws -> Data {
        let fd = try openFile(relativePath: relativePath)
        defer { Darwin.close(fd) }
        var result = Data()
        var buffer = [UInt8](repeating: 0, count: 65_536)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count == 0 { return result }
            if count < 0 {
                if errno == EINTR { continue }
                throw ApprovalError.unsafeFile("could not read \(relativePath)")
            }
            guard result.count <= maximumBytes - count else {
                throw ApprovalError.unsafeFile("\(relativePath) exceeds the size limit")
            }
            result.append(contentsOf: buffer[0 ..< count])
        }
    }

    public func hash(boundReferencePath: String) throws -> FileDigest {
        let components = try Self.safeRelativeComponents(boundReferencePath)
        let projectRelative: [String]
        if components.count >= 3,
           components[0] == "projects",
           components[1] == projectID {
            projectRelative = Array(components.dropFirst(2))
        } else {
            projectRelative = components
        }
        guard !projectRelative.isEmpty else {
            throw ApprovalError.unsafeFile("evidence reference does not name a file")
        }
        return try hash(relativePath: projectRelative.joined(separator: "/"))
    }

    private func openFile(relativePath: String) throws -> Int32 {
        let components = try Self.safeRelativeComponents(relativePath)
        guard let filename = components.last else {
            throw ApprovalError.unsafeFile("empty project-relative path")
        }
        var directoryFD = Darwin.dup(rootFD)
        guard directoryFD >= 0 else {
            throw ApprovalError.unsafeFile("could not duplicate project directory descriptor")
        }
        do {
            for component in components.dropLast() {
                let next = component.withCString {
                    Darwin.openat(directoryFD, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                }
                guard next >= 0 else {
                    throw ApprovalError.unsafeFile("project path contains a missing or indirect directory")
                }
                Darwin.close(directoryFD)
                directoryFD = next
            }
            let fileFD = filename.withCString {
                Darwin.openat(directoryFD, $0, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
            }
            guard fileFD >= 0 else {
                throw ApprovalError.unsafeFile("project file is missing or indirect: \(relativePath)")
            }
            var status = stat()
            guard fstat(fileFD, &status) == 0,
                  status.st_mode & S_IFMT == S_IFREG
            else {
                Darwin.close(fileFD)
                throw ApprovalError.unsafeFile("project path is not a regular file: \(relativePath)")
            }
            Darwin.close(directoryFD)
            return fileFD
        } catch {
            Darwin.close(directoryFD)
            throw error
        }
    }

    private static func openAbsoluteDirectory(_ path: String) throws -> Int32 {
        let components = path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        var fd = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else {
            throw ApprovalError.unsafeFile("could not open the filesystem root")
        }
        do {
            for component in components {
                let next = component.withCString {
                    Darwin.openat(fd, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                }
                guard next >= 0 else {
                    throw ApprovalError.unsafeFile("project_root contains a missing or indirect directory at \(component) (errno \(errno))")
                }
                Darwin.close(fd)
                fd = next
            }
            return fd
        } catch {
            Darwin.close(fd)
            throw error
        }
    }

    private static func safeRelativeComponents(_ path: String) throws -> [String] {
        guard !path.isEmpty, !path.hasPrefix("/"), !path.contains("\\"), !path.contains("\0") else {
            throw ApprovalError.unsafeFile("path must be a non-empty relative POSIX path")
        }
        let components = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
            throw ApprovalError.unsafeFile("path traversal and empty components are forbidden")
        }
        return components
    }
}

public enum DirectFile {
    public static func read(path: String, maximumBytes: Int = 1_048_576) throws -> Data {
        guard path.hasPrefix("/") else {
            throw ApprovalError.unsafeFile("request path must be absolute")
        }
        let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else {
            throw ApprovalError.unsafeFile("request file is missing or indirect")
        }
        defer { Darwin.close(fd) }
        var status = stat()
        guard fstat(fd, &status) == 0,
              status.st_mode & S_IFMT == S_IFREG,
              status.st_size >= 0,
              status.st_size <= maximumBytes
        else {
            throw ApprovalError.unsafeFile("request must be a regular file no larger than 1 MiB")
        }
        var result = Data()
        result.reserveCapacity(Int(status.st_size))
        var buffer = [UInt8](repeating: 0, count: 65_536)
        while true {
            let count = Darwin.read(fd, &buffer, buffer.count)
            if count == 0 { return result }
            if count < 0 {
                if errno == EINTR { continue }
                throw ApprovalError.unsafeFile("could not read request file")
            }
            guard result.count <= maximumBytes - count else {
                throw ApprovalError.unsafeFile("request file exceeds the size limit")
            }
            result.append(contentsOf: buffer[0 ..< count])
        }
    }
}
