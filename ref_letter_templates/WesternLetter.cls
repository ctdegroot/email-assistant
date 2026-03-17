
\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{WesternLetter}[2024/05/07 Western University reference letter class]

\LoadClass[11pt]{letter}

\RequirePackage[top=3cm,bottom=4cm,left=2.54cm,right=2.54cm]{geometry}
\RequirePackage{fancyhdr}
\RequirePackage{graphicx}
\RequirePackage{xcolor}
\RequirePackage{lastpage}

% Footer used on all pages
\fancypagestyle{fancy}{
  \fancyhf{}
  \fancyfoot[C]{\footnotesize
    Department of Mechanical and Materials Engineering, Western University \\
    1151 Richmond St. N., London, ON, Canada N6A 5B9\quad t. 519.661.2136,\quad f. 519.661.3020\quad \texttt{www.westernu.ca}
  }
  \renewcommand{\headrulewidth}{0pt}
  \renewcommand{\footrulewidth}{0pt}
}

% Header + Footer for first page only
\fancypagestyle{firstpage}{
  \fancyhf{}
  \fancyhead[L]{%
    \hspace{-0.25in}\includegraphics[height=1.5cm]{Engineer_Stacked_PurpleGrey.png}
  }
  \fancyhead[R]{%
    \textcolor[HTML]{4F2683}{\large\bfseries Mechanical \& Materials Engineering}
  }
  \fancyfoot[C]{\footnotesize
    Department of Mechanical and Materials Engineering, Western University \\
    1151 Richmond St. N., London, ON, Canada N6A 5B9\quad t. 519.661.2136,\quad f. 519.661.3020\quad \texttt{www.westernu.ca}
  }
  \renewcommand{\headrulewidth}{0pt}
  \renewcommand{\footrulewidth}{0pt}
}

\setlength{\parindent}{0pt}
\setlength{\parskip}{1em}
\setlength{\headheight}{2.5cm}
\setlength{\headsep}{1.5cm}

% Signature block
\newcommand{\closingwithsignature}{%
  \bigskip
  Sincerely,%
  \bigskip

  \includegraphics{Signature.pdf}%

  \textbf{Christopher DeGroot} \\
  Assistant Professor \\
  Department of Mechanical and Materials Engineering \\
  Western University
}
