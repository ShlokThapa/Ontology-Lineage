import { Database, FileText, GitBranch, Link2, MessageSquareText, type LucideIcon } from "lucide-react";

/** The stages of the flow, in order. `group` matches the job groups the backend sends (its order is the same). */
export interface Stage {
  n: number;
  group: "source" | "lineage" | "policies" | "llm" | "use";
  icon: LucideIcon;
  /** Short name for the flow strip; the sections below use the backend's group title. */
  short: string;
  /** Where it runs, in the slide's coloured sub-title. */
  where: string;
  whereTone: "teal" | "orange";
  /** Circle colour behind the icon. */
  circle: string;
  ai?: boolean;
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
    n: 4, group: "llm", icon: Link2, short: "Policy document mapping", where: "Neo4j graph + AI", whereTone: "orange",
    circle: "#E0912F", ai: true, bullets: ["AI links each policy section to the data it governs", "You review before anything is saved"],
  },
  {
    n: 5, group: "use", icon: MessageSquareText, short: "Ask and explore", where: "Chat · Explore", whereTone: "orange",
    circle: "#E0912F", bullets: ["Questions answered with cited policy sections", "Lineage and impact"],
  },
];

export const STAGE_BY_GROUP = new Map(STAGES.map((s) => [s.group, s]));
