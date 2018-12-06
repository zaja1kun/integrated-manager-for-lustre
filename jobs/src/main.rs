enum HostState {
    Undeployed,
    Unconfigured,
    PackagesInstalled,
    Managed,
    Monitored,
    Working,
    Removed,
}

struct Host {
    state: HostState,
}

trait Job {
    type Resource;
    fn can_run(&self, x: &Self::Resource) -> bool;
}

struct RebootHostJob;

impl Job for RebootHostJob {
    type Resource = Host;

    fn can_run(&self, x: &Host) -> bool {
        let state = match x.state {
            HostState::Removed | HostState::Undeployed | HostState::Unconfigured => false,
            _ => true,
        };

        state
    }
}

fn main() {
    println!("Hello, world!");
}
