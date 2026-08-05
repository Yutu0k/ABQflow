C=======================================================================
C UMAT: isotropic linear elastic material (Standard, plane stress capable)
C
C Adapted from the ABQflow subroutine skill reference:
C   reference/abaqus_skills/abaqus_subroutine_skills/reference/material/umat_elastic.md
C
C PROPS(1) = E   (Young's modulus)
C PROPS(2) = NU  (Poisson's ratio)
C No state variables (NSTATV = 0, no *Depvar needed).
C
C Handles NDI = 3 (3D / plane strain / axisymmetric) and NDI = 2
C (plane stress) — the ABQflow planar-stress example uses CPS3/CPS4R
C elements, which fall into the NDI = 2 branch.
C=======================================================================
      SUBROUTINE UMAT(STRESS,STATEV,DDSDDE,SSE,SPD,SCD,
     1 RPL,DDSDDT,DRPLDE,DRPLDT,
     2 STRAN,DSTRAN,TIME,DTIME,TEMP,DTEMP,
     3 PREDEF,DPRED,CMNAME,NDI,NSHR,NTENS,NSTATV,
     4 PROPS,NPROPS,COORDS,DROT,PNEWDT,
     5 CELENT,DFGRD0,DFGRD1,NOEL,NPT,LAYER,KSPT,
     6 KSTEP,KINC)
C
      INCLUDE 'ABA_PARAM.INC'
C
      CHARACTER*80 CMNAME
      DIMENSION STRESS(NTENS),STATEV(NSTATV),
     1 DDSDDE(NTENS,NTENS),DDSDDT(NTENS),DRPLDE(NTENS),
     2 STRAN(NTENS),DSTRAN(NTENS),TIME(2),PREDEF(1),DPRED(1),
     3 COORDS(3),DROT(3,3),DFGRD0(3,3),DFGRD1(3,3)
C
      REAL*8 E, NU, EG, EG2, ELAM, FAC
      INTEGER I, J
C
C-----------------------------------------------------------------------
C  Read material parameters
C-----------------------------------------------------------------------
      E    = PROPS(1)
      NU   = PROPS(2)
C
C-----------------------------------------------------------------------
C  Elastic constants
C-----------------------------------------------------------------------
      EG2  = E / (1.0D0 + NU)
      EG   = EG2 / 2.0D0
      ELAM = E * NU / ((1.0D0 + NU) * (1.0D0 - 2.0D0*NU))
C
C-----------------------------------------------------------------------
C  Initialize Jacobian matrix
C-----------------------------------------------------------------------
      DO I = 1, NTENS
        DO J = 1, NTENS
          DDSDDE(I,J) = 0.0D0
        END DO
      END DO
C
C-----------------------------------------------------------------------
C  Assemble Jacobian matrix
C-----------------------------------------------------------------------
      IF (NDI .EQ. 3) THEN
C       3D or plane strain case
        FAC = ELAM + EG2
        DDSDDE(1,1) = FAC
        DDSDDE(2,2) = FAC
        DDSDDE(3,3) = FAC
        DDSDDE(1,2) = ELAM
        DDSDDE(1,3) = ELAM
        DDSDDE(2,1) = ELAM
        DDSDDE(2,3) = ELAM
        DDSDDE(3,1) = ELAM
        DDSDDE(3,2) = ELAM
        DDSDDE(4,4) = EG
        IF (NSHR .GE. 2) THEN
          DDSDDE(5,5) = EG
          DDSDDE(6,6) = EG
        END IF
      ELSE IF (NDI .EQ. 2) THEN
C       Plane stress case (CPS3 / CPS4R elements)
        FAC = E / (1.0D0 - NU*NU)
        DDSDDE(1,1) = FAC
        DDSDDE(2,2) = FAC
        DDSDDE(1,2) = FAC * NU
        DDSDDE(2,1) = FAC * NU
        DDSDDE(3,3) = FAC * (1.0D0 - NU) / 2.0D0
      END IF
C
C-----------------------------------------------------------------------
C  Stress update: sigma_new = sigma_old + D : d(epsilon)
C-----------------------------------------------------------------------
      DO I = 1, NTENS
        DO J = 1, NTENS
          STRESS(I) = STRESS(I) + DDSDDE(I,J) * DSTRAN(J)
        END DO
      END DO
C
C-----------------------------------------------------------------------
C  Strain energy (optional, for *Energy Output)
C-----------------------------------------------------------------------
      SSE = 0.0D0
      DO I = 1, NTENS
        SSE = SSE + STRESS(I) * (STRAN(I) + 0.5D0*DSTRAN(I))
      END DO
      SSE = SSE / 2.0D0
C
      RETURN
      END
