import { useCallback, useEffect, useState } from "react";
import { fetchSetupStatus, retrySetup } from "../api";
import type { SetupStatus } from "../types";

const POLL_MS = 1000;

export interface SetupApi {
  status: SetupStatus | null;
  unreachable: boolean;
  entered: boolean;
  update: (status: SetupStatus) => void;
  retry: () => void;
}

export function useSetup(): SetupApi {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [entered, setEntered] = useState(false);

  const update = useCallback((next: SetupStatus) => {
    setStatus(next);
    setUnreachable(false);
    if (next.ready) setEntered(true);
  }, []);

  useEffect(() => {
    if (entered) return undefined;
    let active = true;
    const load = () => {
      fetchSetupStatus().then(
        (next) => {
          if (active) update(next);
        },
        () => {
          if (active) setUnreachable(true);
        },
      );
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [entered, update]);

  const retry = useCallback(() => {
    retrySetup().then(update, () => setUnreachable(true));
  }, [update]);

  return { status, unreachable, entered, update, retry };
}
