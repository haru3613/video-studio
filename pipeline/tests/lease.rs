use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, UNIX_EPOCH};

use pipeline::{ProjectStore, StoreError};
use tempfile::tempdir;

#[test]
fn claim_does_not_create_a_missing_project() {
    let root = tempdir().unwrap();
    let missing = root.path().join("typo");
    assert!(matches!(
        ProjectStore::new(&missing).claim_at(
            "hermes",
            Duration::from_secs(60),
            UNIX_EPOCH + Duration::from_secs(900)
        ),
        Err(StoreError::InvalidProject(path)) if path == missing
    ));
    assert!(!missing.exists());
}

#[test]
fn expired_lease_can_be_recovered_but_stale_owner_cannot_release_it() {
    let root = tempdir().unwrap();
    let store = ProjectStore::new(root.path());
    let started = UNIX_EPOCH + Duration::from_secs(1_000);

    let first = store
        .claim_at("openclaw", Duration::from_secs(60), started)
        .unwrap();
    let held = store
        .claim_at(
            "hermes",
            Duration::from_secs(60),
            started + Duration::from_secs(10),
        )
        .unwrap_err();
    assert!(matches!(
        held,
        StoreError::LeaseHeld {
            ref owner,
            expires_at: 1_060
        } if owner == "openclaw"
    ));

    let recovered = store
        .claim_at(
            "hermes",
            Duration::from_secs(60),
            started + Duration::from_secs(61),
        )
        .unwrap();
    assert_eq!(recovered.owner, "hermes");
    assert_ne!(recovered.token, first.token);

    assert!(matches!(
        store.release_at("openclaw", &first.token, started + Duration::from_secs(61)),
        Err(StoreError::LeaseMismatch)
    ));
    store
        .release_at(
            "hermes",
            &recovered.token,
            started + Duration::from_secs(62),
        )
        .unwrap();
    assert!(store.current_lease().unwrap().is_none());
}

#[test]
fn owner_can_renew_an_active_lease() {
    let root = tempdir().unwrap();
    let store = ProjectStore::new(root.path());
    let started = UNIX_EPOCH + Duration::from_secs(3_000);
    let lease = store
        .claim_at("hermes", Duration::from_secs(10), started)
        .unwrap();

    let renewed = store
        .renew_at(
            "hermes",
            &lease.token,
            Duration::from_secs(30),
            started + Duration::from_secs(5),
        )
        .unwrap();
    assert_eq!(renewed.token, lease.token);
    assert_eq!(renewed.claimed_at, lease.claimed_at);
    assert_eq!(renewed.expires_at, 3_035);
    assert!(matches!(
        store.claim_at(
            "openclaw",
            Duration::from_secs(10),
            started + Duration::from_secs(11)
        ),
        Err(StoreError::LeaseHeld {
            ref owner,
            expires_at: 3_035
        }) if owner == "hermes"
    ));
    assert!(matches!(
        store.renew_at(
            "hermes",
            "wrong-token",
            Duration::from_secs(30),
            started + Duration::from_secs(6)
        ),
        Err(StoreError::LeaseMismatch)
    ));
    assert!(matches!(
        store.renew_at(
            "hermes",
            &lease.token,
            Duration::from_secs(30),
            started + Duration::from_secs(36)
        ),
        Err(StoreError::LeaseExpired)
    ));
    assert!(matches!(
        store.release_at("hermes", &lease.token, started + Duration::from_secs(36)),
        Err(StoreError::LeaseExpired)
    ));
}

#[test]
fn concurrent_claims_have_exactly_one_winner() {
    let root = tempdir().unwrap();
    let store = Arc::new(ProjectStore::new(root.path()));
    let barrier = Arc::new(Barrier::new(3));
    let started = UNIX_EPOCH + Duration::from_secs(2_000);

    let handles: Vec<_> = ["openclaw", "hermes"]
        .into_iter()
        .map(|owner| {
            let store = Arc::clone(&store);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                store.claim_at(owner, Duration::from_secs(60), started)
            })
        })
        .collect();

    barrier.wait();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(result, Err(StoreError::LeaseHeld { .. })))
            .count(),
        1
    );
}
