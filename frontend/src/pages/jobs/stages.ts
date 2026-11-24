import { Database, FileText, GitBranch, Link2, MessageSquareText, Network, type LucideIcon } from "lucide-react";

/** The stages of the flow, in order. `group` matches the job groups the backend sends (its order is the same). */
export interface Stage {
  n: number;
  group: "source" | "lineage" | "policies" | "llm" | "graph" | "use";
  icon: LucideIcon;
  /** Short name for the flow strip; the sections below use the backend's group title. */
  short: string;
  /** Where it runs, in the slide's coloured sub-title. Left out when there is nothing useful to say. */
  where?: string;
  whereTone: "teal" | "orange";
  /** Circle colour behind the icon. */
  circle: string;
  ai?: boolean;
  /** Shown on the flow only: no button, no tab, no pipeline behind it. */
  display_only?: boolean;
  bullets: string[];
}

export const STAGES: Stage[] = [
  {
    n: 1, group: "source", icon: Database, short: "Structured data harvesting", where: "PostgreSQL → Fabric pipelines", whereTone: "teal",
    circle: "#1F2A44", bullets: ["Copies the banking tables into Fabric", "Accounts, branches, customers, loans, transactions"],
  },
  {
    n: 2, group: "lineage", icon: GitBranch, short: "Lineage extraction", where: "Fabric notebook", whereTone: "teal",
    circle: "#0F7C86", bullets: ["Finds where each column comes from", "Loads that map into the graph"],
  },
  {
    n: 3, group: "policies", icon: FileText, short: "Unstructured data chunking", where: "Word policies → Fabric notebooks", whereTone: "teal",
    circle: "#4B5568", bullets: ["Cuts each policy into sections", "Makes them searchable"],
  },
  {
    n: 4, group: "llm", icon: Link2, short: "Policy document mapping", whereTone: "orange",
    circle: "#E0912F", ai: true, bullets: ["AI links each policy section to the data it governs", "You review before anything is saved"],
  },
  {
    n: 5, group: "graph", icon: Network, short: "Graph modelling", whereTone: "teal", display_only: true,
    circle: "#5B6B8C", bullets: ["Connects tables, columns and policies into one graph", "Shows how everything relates"],
  },
  {
    n: 6, group: "use", icon: MessageSquareText, short: "Consumption", where: "Chat · Explore", whereTone: "orange",
    circle: "#E0912F", bullets: ["Questions answered with cited policy sections", "Lineage and impact"],
  },
];

export const STAGE_BY_GROUP = new Map(STAGES.map((s) => [s.group, s]));
