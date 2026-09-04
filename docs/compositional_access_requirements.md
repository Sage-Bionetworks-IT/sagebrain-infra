# Compositional Access Requirements: A Building-Block Approach to Data Access

## Think of Access Requirements Like LEGO® Blocks

You're already managing access to **millions of research files** across Synapse today. As we extend governance to the knowledge graph and beyond, we need a smarter approach. Instead of writing the same rules over and over for each file, we can create **reusable building blocks** that snap together in different combinations—like LEGO bricks.

---

## The Problem with Today's Approach

### How We Do It Now: Writing Rules for Every File

Right now, each dataset gets its own list of access requirements. Here's what that looks like:

**ADNI Imaging Study Files (syn123):**
- Complete ADNI training program
- Complete MRI imaging safety course
- Complete HIPAA privacy training
- Get Stanford IRB approval

**ADNI Genomics Study Files (syn456):**
- Complete ADNI training program
- Complete genomics data handling course
- Complete HIPAA privacy training
- Get Stanford IRB approval
- Get dbGaP authorization

**PPMI Imaging Study Files (syn789):**
- Complete PPMI training program
- Complete MRI imaging safety course
- Complete HIPAA privacy training
- Get University of Pennsylvania IRB approval

### Why This Doesn't Work Well

**❌ We repeat ourselves constantly**
HIPAA training is listed 3 times. MRI safety is listed twice. Every study has its own training, even when studies are similar.

**❌ We can't easily reuse combinations**
If we add 100 new ADNI imaging files, we have to copy-paste those same 4 requirements 100 times.

**❌ It doesn't scale**
With 400,000+ files, we'd be managing millions of individual requirements.

**❌ Researchers can't see the big picture**
"What do I need to access all ADNI data?" is impossible to answer without checking every single file.

---

## Solution: Building Blocks That Snap Together

### The Four Types of Building Blocks

Instead of listing requirements for every file, we create **reusable building blocks** that represent different kinds of requirements. Files then just say "I need these blocks" and snap them together.

#### **Block Type 1: Study/Program Requirements**
Each study or program has its own requirements based on the site's IRB and consortium rules.

**ADNI Study Block:**
- Complete ADNI-specific training
- Sign ADNI Data Use Agreement
- Get approved by Stanford IRB (ADNI's coordinating site)

**PPMI Study Block:**
- Complete PPMI-specific training
- Sign PPMI Data Use Certificate
- Get approved by University of Pennsylvania IRB (PPMI's coordinating site)

**NF Study Block:**
- Complete NF Open Science training
- Acknowledge open data terms
- Get approved by site-specific IRB (varies by institution)

#### **Block Type 2: Data Type Requirements**
Different types of data need different safety/ethics training.

**Genomics Data Block:**
- Complete genomics ethics training
- Get dbGaP authorization (for controlled-access genomics)

**Imaging Data Block:**
- Complete MRI/imaging safety training
- Complete radiology data handling course

**Clinical Data Block:**
- Complete clinical research ethics training

#### **Block Type 3: Privacy/Compliance Requirements**
These are the legal/regulatory foundations.

**HIPAA Block:**
- Complete HIPAA privacy training
- Maintain current HIPAA certification

**GDPR Block:**
- Complete GDPR training (for European data)
- Acknowledge international data sharing rules

#### **Block Type 4: Access Level Requirements**
Different actions need different permissions.

**Download Block:**
- Acknowledge data use limitations
- Agree to no redistribution

**Compute Block:**
- Certified for cloud compute environment
- Agree to analysis-only terms

### How Files Use These Blocks

Instead of writing out full requirements, each file just says which blocks it needs:

**ADNI Imaging Study File (syn123):**
```
This file needs:
  🧱 ADNI Study Block
  🧱 Imaging Data Block
  🧱 HIPAA Block
```

**ADNI Genomics Study File (syn456):**
```
This file needs:
  🧱 ADNI Study Block
  🧱 Genomics Data Block
  🧱 HIPAA Block
```

**PPMI Imaging Study File (syn789):**
```
This file needs:
  🧱 PPMI Study Block
  🧱 Imaging Data Block
  🧱 HIPAA Block
```

**NF Open Data Study File (syn999):**
```
This file needs:
  🧱 NF Study Block
  🧱 Clinical Data Block
  🧱 HIPAA Block
```

### Why This Is Better

**✅ We define each block once, use it everywhere**
HIPAA Block is defined one time. When HIPAA training requirements change, we update one block and all millions of files using it are automatically updated.

**✅ Blocks can be mixed and matched**
ADNI Imaging = ADNI Block + Imaging Block + HIPAA Block
ADNI Genomics = ADNI Block + Genomics Block + HIPAA Block
(ADNI Block and HIPAA Block are reused!)

**✅ Easy to add new files**
Adding 10,000 new PPMI files? Just tag them with the 3 blocks. No copy-pasting.

**✅ Researchers see clear requirements**
"What do I need for all ADNI data?" → "Complete these 3 blocks: ADNI Study + HIPAA + [your data type]"

---

## How It Works When Researchers Need Multiple Files

### Example: Researcher Wants Both ADNI Imaging and ADNI Genomics Files

**What they're requesting:**
- ADNI Imaging files (syn123) → needs: **ADNI Block + Imaging Block + HIPAA Block**
- ADNI Genomics files (syn456) → needs: **ADNI Block + Genomics Block + HIPAA Block**

**The system automatically combines the requirements:**
```
To access both file sets, you need these unique blocks:

  🧱 ADNI Study Block (shared by both)
  🧱 Imaging Data Block (for imaging files)
  🧱 Genomics Data Block (for genomics files)
  🧱 HIPAA Block (shared by both)
```

Notice: ADNI Block and HIPAA Block only appear once, even though both file sets need them!

### What the Researcher Sees

**If they have everything:**
```
✅ ADNI training: Complete
✅ Imaging safety: Complete
✅ Genomics ethics: Complete
✅ HIPAA certification: Current
✅ Stanford IRB: Approved

All requirements met! Access granted to 10,250 files.
```

**If they're missing something:**
```
✅ ADNI training: Complete
✅ Imaging safety: Complete
✅ HIPAA certification: Current
❌ Genomics ethics: Not started
❌ Stanford IRB: Pending approval

Complete the missing requirements to unlock both imaging and genomics files.

Right now, you can access: 5,420 imaging files
After completing requirements: 10,250 total files (imaging + genomics)
```

This way, researchers always know exactly what they need and what they'll unlock by completing each requirement.

---

## Specialized Blocks: When Basic Isn't Enough

### Building Blocks Can Have Sub-Types

Sometimes a basic block needs extra requirements for specialized data. Think of these like LEGO Technic pieces—they build on the basic blocks with additional features.

**Example: Imaging Data Has Subtypes**

**Basic Imaging Block:** MRI safety training

**fMRI Imaging Block** (builds on basic):
- Everything from basic Imaging Block
- PLUS: fMRI analysis training
- PLUS: Task design certification

**PET Imaging Block** (builds on basic):
- Everything from basic Imaging Block
- PLUS: PET safety training
- PLUS: Radioisotope handling certification

### What This Means for Files

**Basic ADNI MRI Study (syn123):**
```
Needs: ADNI Block + Imaging Block + HIPAA Block
```

**Advanced ADNI fMRI Study (syn999):**
```
Needs: ADNI Block + fMRI Imaging Block + HIPAA Block
                      ↑
            (includes basic imaging requirements + fMRI extras)
```

Researchers automatically get credit for completing the basic training when they complete the specialized version!

---

## Multi-Data-Type Studies

### When One Study Has Many Types of Data

Some studies collect multiple types of data. Instead of creating a giant custom block for each study, we just combine the data type blocks we need.

**ADNI Multi-Modal Biomarker Study (syn2000):**
```
This study includes 4 types of data, so it needs:
  🧱 ADNI Study Block (the study-specific requirements)
  🧱 Genomics Data Block
  🧱 Imaging Data Block
  🧱 Clinical Data Block
  🧱 Proteomics Data Block
  🧱 HIPAA Block
```

**Researcher's View:**
```
To access this multi-modal study, complete:

Already done:
✅ ADNI training
✅ Genomics ethics
✅ dbGaP authorization
✅ HIPAA certification

Still need:
❌ Imaging safety training
❌ Radiology data handling
❌ Clinical research ethics
❌ Proteomics training

Complete 4 more trainings to unlock all data types in this study.
```

This way, a researcher who's already qualified for genomics and clinical data can see they only need imaging and proteomics training to access the full dataset—no redundant requirements!

---

## Study Membership: One Block, Many Benefits

### Some Blocks Unlock Multiple Requirements at Once

When a researcher joins a study consortium (like ADNI or PPMI), they complete a comprehensive training program that covers multiple requirements. We can represent this as a single block that bundles together several requirements.

**ADNI Study Block** (bundle):
- ADNI-specific training (covers study protocols + data handling)
- Access to imaging data
- Access to clinical data
- Signed Data Use Certificate
- Stanford IRB approval

**PPMI Study Block** (bundle):
- PPMI-specific training (covers study protocols + data handling)
- Access to imaging data
- Access to clinical data
- Access to genomics data
- Signed Data Use Certificate
- UPenn IRB approval

### What This Means for Researchers

**Researcher joins ADNI Consortium:**

Before joining:
```
To access ADNI data, you need:
❌ ADNI training
❌ Imaging data access
❌ Clinical data access
❌ HIPAA certification
❌ IRB approval
```

After completing ADNI membership (one process):
```
✅ ADNI training
✅ Imaging data access
✅ Clinical data access
✅ IRB approval
❌ HIPAA certification (still need this separately)
```

**The benefit:** One consortium membership process checks off multiple boxes at once. The researcher only needs to complete the remaining standalone requirements (like HIPAA) to get full access.

This reflects how study memberships actually work—joining a study isn't just one requirement, it's passing a bundle of requirements together!

---

## How the System Enforces Access (Behind the Scenes)

### Automatic Filtering

When a researcher searches for or requests files, the system automatically filters results based on which blocks they've completed.

**Example:**
Researcher has completed: ADNI Block, Imaging Block, HIPAA Block

When they search "show me all available files," the system:
1. Looks at every file's required blocks
2. Only shows files where ALL required blocks match what the researcher has completed
3. Hides everything else

**They'll see:**
- ✅ ADNI Imaging files (needs: ADNI + Imaging + HIPAA) ← Perfect match!
- ❌ ADNI Genomics files (needs: ADNI + Genomics + HIPAA) ← Missing Genomics Block
- ❌ PPMI Imaging files (needs: PPMI + Imaging + HIPAA) ← Missing PPMI Block

The researcher never sees files they can't access—no frustrating "Access Denied" messages!

### Access Certificates

When a researcher completes all required blocks for a set of files, the system issues them an **access certificate**—like a digital key that says "this person can access files with these specific blocks."

**Example Certificate:**
```
Researcher: Dr. Jane Smith (ID: 9000001)
Valid until: January 15, 2027

Approved for files requiring:
  ✓ ADNI Study Block
  ✓ Imaging Data Block
  ✓ Genomics Data Block
  ✓ HIPAA Block

Can perform: Download, Query, Compute

Access Summary: "ADNI Imaging & Genomics with HIPAA"
```

The certificate is checked automatically every time they try to access a file. If a file needs blocks they don't have, access is denied. If all blocks match, access is granted instantly.

---

## How Governance Teams Would Use This System

### Setting Up Blocks (Do This Once)

The first time you set up the system, you create your library of reusable blocks. This is like setting up your LEGO collection—you do it once, then reuse the pieces forever.

**Step 1: Create Study Blocks**

For each study/program:
```
Create Block: "ADNI Study"
  - Name: ADNI Alzheimer's Disease Neuroimaging Initiative
  - Required training: ADNI Protocol Training
  - Required agreement: ADNI Data Use Certificate
  - IRB site: Stanford University
  - Contact: adni-admin@example.org
```

**Step 2: Create Data Type Blocks**

For each type of data:
```
Create Block: "Genomics Data"
  - Name: Genomic/Genetic Data Access
  - Required training: Genomics Ethics (CITI Module)
  - Required certification: dbGaP Authorized User
  - Renewal: Annual
```

**Step 3: Create Compliance Blocks**

For regulatory requirements:
```
Create Block: "HIPAA Compliance"
  - Name: HIPAA Privacy & Security
  - Required training: HIPAA Certification Course
  - Renewal: Every 2 years
  - Verification: Certificate upload required
```

### Adding New Files (Easy and Fast)

When a new study uploads files, you just tag them with the appropriate blocks:

**Example: ADNI uploads 5,000 new MRI scans**

```
Select files: syn52345001 through syn52350000
Assign blocks:
  🧱 ADNI Study Block
  🧱 Imaging Data Block
  🧱 HIPAA Block

Save → All 5,000 files now have access requirements!
```

That's it! No writing custom requirements for 5,000 individual files.

### When Researchers Request Access

**Researcher submits access request for ADNI files:**

System automatically checks their profile:
```
Researcher Profile: Dr. Jane Smith

Completed blocks:
  ✅ ADNI Study Block (completed Jan 2026)
  ✅ Imaging Data Block (completed Feb 2026)
  ✅ HIPAA Block (expires Dec 2027)

Missing blocks:
  (none)

Decision: AUTO-APPROVED
Files unlocked: 125,420 ADNI imaging files
```

**Another researcher requests same files:**

```
Researcher Profile: Dr. John Doe

Completed blocks:
  ✅ ADNI Study Block (completed Mar 2026)
  ✅ HIPAA Block (expires Jun 2027)

Missing blocks:
  ❌ Imaging Data Block

Decision: PENDING
Next step: Complete imaging safety training
Files available after completion: 125,420 ADNI imaging files
```

The system does all the checking automatically. As a governance team member, you only get involved if there's something unusual or if the researcher has questions.

---

## Why This Approach is Better: Real Benefits

### 1. **Handles Millions of Files Easily**

**Old way:**
400,000 files × 5 requirements each = 2,000,000 individual requirements to manage

**New way:**
~20 reusable blocks that cover all 400,000 files
Check each researcher against ~10 blocks = thousands of times less work

### 2. **Update Once, Apply Everywhere**

**Real scenario:** HIPAA certification requirements change

**Old way:**
- Find all 450,000 files with HIPAA requirements
- Update each file's requirements individually
- Risk missing some files
- Takes days or weeks

**New way:**
- Update the one HIPAA Block definition
- All 450,000 files using that block automatically updated
- Impossible to miss files
- Takes 5 minutes

### 3. **Researchers Understand What They Need**

**Researcher question:** "What do I need to access ADNI imaging data?"

**Old way:**
"You need to check the access requirements on each file. Most require AR001, AR002, AR003, AR004, and AR012..."

**New way:**
"You need 3 things:
1. ADNI Study Block (includes study training + IRB)
2. Imaging Data Block (imaging safety training)
3. HIPAA Block (HIPAA certification)

Complete these trainings and you'll unlock 45,000+ ADNI imaging files."

### 4. **Automatically Show What They Can Access**

When researchers search, they only see files they qualify for:

**Researcher completed: ADNI Block + HIPAA Block only**
- They see: 23,000 files they CAN access (ADNI clinical data)
- They don't see: 22,000 files they CAN'T access yet (ADNI imaging/genomics)
- System shows: "Complete Imaging Block to unlock 15,000 more files"

No frustrating "Access Denied" messages. No wasting time requesting files they can't have yet.

### 5. **Study Membership Works Like It Should**

**When someone joins ADNI Consortium:**

**Old way:**
- Manually approve them for 45,000 individual ADNI files
- Hope you don't miss any
- Repeat when new ADNI files are added

**New way:**
- ADNI membership completes several blocks at once
- Automatically grants access to all current AND future ADNI files
- No manual approvals needed

---

## Transitioning from Current System to Building Blocks

### How We Get There

You don't have to change everything overnight. Here's a practical migration path:

**Phase 1: Identify the Patterns (1-2 weeks)**
- Look at your current access requirements across all files
- Group similar requirements together:
  - Which ones are study-specific? (ADNI, PPMI, NF, etc.)
  - Which ones are about data types? (genomics, imaging, clinical)
  - Which ones are compliance? (HIPAA, GDPR, IRB)
- Create a list of ~20-30 common patterns

**Phase 2: Create the Blocks (2-3 weeks)**
- Turn each pattern into a reusable block
- Define what training/certifications each block requires
- Set up renewal periods where applicable

**Example mapping:**
```
Current AR001 "ADNI Training" → becomes "ADNI Study Block"
Current AR002 "MRI Safety" → becomes "Imaging Data Block"
Current AR003 "HIPAA Training" → becomes "HIPAA Block"
Current AR005 "Genomics Ethics" → becomes "Genomics Data Block"
```

**Phase 3: Tag Existing Files (Can be automated)**
- For each file, replace individual ARs with the corresponding blocks
- Example: File that had AR001 + AR002 + AR003 → now tagged with ADNI Block + Imaging Block + HIPAA Block
- This step can be done automatically by the system based on the current ARs

**Phase 4: New Files Use Blocks (Ongoing)**
- All new files get tagged with blocks from day one
- Much faster than writing custom requirements

---

## Summary: The Building Block Approach

### The Core Idea

Instead of writing access requirements separately for millions of files, we create **reusable building blocks** that represent common requirements. Files just say which blocks they need, and blocks snap together like LEGO pieces.

### The Four Types of Blocks

1. **Study/Program Blocks** – Study-specific requirements and IRB approvals (ADNI, PPMI, NF, etc.)
2. **Data Type Blocks** – Safety and ethics training for different data types (Genomics, Imaging, Clinical, etc.)
3. **Compliance Blocks** – Legal and regulatory foundations (HIPAA, GDPR, etc.)
4. **Access Level Blocks** – Different permissions for different actions (Download, Compute, etc.)

### How It Works

- **Files are tagged with blocks:** "This file needs ADNI Block + Imaging Block + HIPAA Block"
- **Researchers complete blocks:** "I've finished ADNI training, imaging safety, and HIPAA certification"
- **System matches automatically:** If researcher's blocks match file's blocks → access granted
- **When accessing multiple files:** System combines all required blocks and shows what's needed

### Why Governance Teams Should Care

✅ **Scales to millions of files** – Manage 20 blocks instead of millions of individual requirements

✅ **Update once, apply everywhere** – Change HIPAA requirements in one place, all files updated instantly

✅ **Clear for researchers** – "You need these 3 blocks" instead of confusing individual requirements

✅ **No repetitive work** – New files? Just tag them with existing blocks. Takes seconds, not hours.

✅ **Study membership works right** – Joining a consortium automatically unlocks thousands of files

### Bottom Line

This is about working smarter, not harder. You're already managing millions of files successfully in Synapse. As you extend governance to the knowledge graph and beyond, building blocks let you scale without drowning in repetitive work.

**Think LEGO, not handcrafted custom rules for every single file.**
