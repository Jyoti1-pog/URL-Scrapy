import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/** Config and health barely change within a session, so they are fetched once
 *  and shared. Both are cheap; neither touches the network beyond this host. */
export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: api.config, staleTime: Infinity });
}

export function useHealth() {
  return useQuery({ queryKey: ["health"], queryFn: api.health, staleTime: 60_000 });
}
