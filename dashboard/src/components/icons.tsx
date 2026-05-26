"use client";
// Re-export lucide-react icons under the prototype's naming.
// The prototype used a custom Icons.* object; this maps each name 1:1.
import {
  Activity, AlertTriangle, AlignLeft, ArrowDown, ArrowRight, ArrowUp,
  Bell, Book, Calendar, Check, ChevronDown, ChevronLeft, ChevronRight,
  ChevronsUpDown, Clock, Code, Command, Copy, Cpu, Database,
  DollarSign, Download, Pencil, ExternalLink, Eye, EyeOff, FileCheck,
  Filter, GitBranch, Globe, LayoutGrid, Hash, HelpCircle, Key, Languages,
  Layers, List, Lock, LogOut, Moon, MoreHorizontal, PanelLeft, Pause,
  Pin, Play, Plus, RefreshCw, RotateCw, Save, Search, Settings, Shield,
  ShieldCheck, Shuffle, Sparkles, Sun, Trash2, UserCheck, Zap,
  Unlock, Bot, ScrollText, MessageSquare, User, CreditCard, Camera, Terminal, Menu,
  X, ChevronUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export const Icons = {
  Activity, AlertTriangle, AlignLeft, ArrowDown, ArrowRight, ArrowUp,
  Bell, Book, Calendar, Check, ChevronDown, ChevronLeft, ChevronRight,
  ChevronsUpDown, Clock, Code, Command, Copy, Cpu, Database,
  Dollar: DollarSign, Download, Edit: Pencil, ExternalLink, Eye, EyeOff,
  FileCheck, Filter, GitBranch, Globe, Grid: LayoutGrid, Hash, HelpCircle,
  Key, Languages, Layers, List, Lock, LogOut, Moon, MoreHorizontal,
  PanelLeft, Pause, Pin, Play, Plus, RefreshCw, RotateCw, Save, Search,
  Settings, Shield, ShieldCheck, Shuffle, Sparkles, Sun,
  Trash: Trash2, UserCheck, Zap, Unlock, Bot, ScrollText, MessageSquare,
  Slack: MessageSquare,
  User, CreditCard, Camera, Terminal, Menu,
  X, ChevronUp,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof Icons;
